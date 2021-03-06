# coding=utf-8
# Copyright 2015 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

import collections
from os.path import basename as os_path_basename

import six

from pants.base.specs import DescendantAddresses, SiblingAddresses, SingleAddress
from pants.build_graph.address import Address
from pants.engine.exp.addressable import AddressableDescriptor, Addresses, TypeConstraintError
from pants.engine.exp.fs import DirectoryListing, FilesContent, Path, Paths, RecursiveSubDirectories
from pants.engine.exp.mapper import AddressFamily, AddressMap, AddressMapper, ResolveError
from pants.engine.exp.objects import Locatable, SerializableFactory, Validatable
from pants.engine.exp.selectors import Select, SelectDependencies, SelectLiteral, SelectProjection
from pants.engine.exp.struct import Struct
from pants.util.objects import datatype


class ResolvedTypeMismatchError(ResolveError):
  """Indicates a resolved object was not of the expected type."""


def _key_func(entry):
  key, value = entry
  return key


class BuildFilePaths(datatype('BuildFilePaths', ['paths'])):
  """A list of Paths that are known to match a BUILD file pattern.

  TODO: Because BUILD file names are matched using a regex, this cannot currently use PathGlobs.
  If we were willing to allow a bit of slop in terms of files read, this could use
  PathGlobs to get FilesContent for all files with the right prefix, and then discard.
  """


def filter_buildfile_paths(address_mapper, directory_listing):
  build_files = tuple(f for f in directory_listing.files
                      if address_mapper.build_pattern.match(os_path_basename(f.path)))
  return BuildFilePaths(build_files)


def parse_address_family(address_mapper, path, build_files_content):
  """Given the contents of the build files in one directory, return an AddressFamily.

  The AddressFamily may be empty, but it will not be None.
  """
  address_maps = []
  for filepath, filecontent in build_files_content.dependencies:
    address_maps.append(AddressMap.parse(filepath,
                                         filecontent,
                                         address_mapper.symbol_table_cls,
                                         address_mapper.parser_cls))
  return AddressFamily.create(path.path, address_maps)


class UnhydratedStruct(datatype('UnhydratedStruct', ['address', 'struct', 'dependencies'])):
  """A product type that holds a Struct which has not yet been hydrated.

  A Struct counts as "hydrated" when all of its members (which are not themselves dependencies
  lists) have been resolved from the graph. This means that hyrating a struct is eager in terms
  of inline addressable fields, but lazy in terms of the complete graph walk represented by
  the `dependencies` field of StructWithDeps.
  """

  def __eq__(self, other):
    if type(self) != type(other):
      return NotImplemented
    return self.struct == other.struct

  def __ne__(self, other):
    return not (self == other)

  def __hash__(self):
    return hash(self.struct)


def resolve_unhydrated_struct(address_family, address):
  """Given an Address and its AddressFamily, resolve an UnhydratedStruct.

  Recursively collects any embedded addressables within the Struct, but will not walk into a
  dependencies field, since those are requested explicitly by tasks using SelectDependencies.
  """

  struct = address_family.addressables.get(address)
  if not struct:
    possibilities = '\n  '.join(str(a) for a in address_family.addressables)
    raise ResolveError('A Struct was not found at address {}. '
                       'Did you mean one of?:\n  {}'.format(address, possibilities))

  dependencies = []
  def maybe_append(outer_key, value):
    if isinstance(value, six.string_types):
      if outer_key != 'dependencies':
        dependencies.append(Address.parse(value, relative_to=address.spec_path))
    elif isinstance(value, Struct):
      collect_dependencies(value)

  def collect_dependencies(item):
    for key, value in sorted(item._asdict().items(), key=_key_func):
      if not AddressableDescriptor.is_addressable(item, key):
        continue
      if isinstance(value, collections.MutableMapping):
        for _, v in sorted(value.items(), key=_key_func):
          maybe_append(key, v)
      elif isinstance(value, collections.MutableSequence):
        for v in value:
          maybe_append(key, v)
      else:
        maybe_append(key, value)

  collect_dependencies(struct)
  return UnhydratedStruct(address, struct, dependencies)


def hydrate_struct(unhydrated_struct, dependencies):
  """Hydrates a Struct from an UnhydratedStruct and its satisfied embedded addressable deps.

  Note that this relies on the guarantee that DependenciesNode provides dependencies in the
  order they were requested.
  """
  address = unhydrated_struct.address
  struct = unhydrated_struct.struct

  def maybe_consume(outer_key, value):
    if isinstance(value, six.string_types):
      if outer_key == 'dependencies':
        # Don't recurse into the dependencies field of a Struct, since those will be explicitly
        # requested by tasks. But do ensure that their addresses are absolute, since we're
        # about to lose the context in which they were declared.
        value = Address.parse(value, relative_to=address.spec_path)
      else:
        value = dependencies[maybe_consume.idx]
        maybe_consume.idx += 1
    elif isinstance(value, Struct):
      value = consume_dependencies(value)
    return value
  # NB: Some pythons throw an UnboundLocalError for `idx` if it is a simple local variable.
  maybe_consume.idx = 0

  # 'zip' the previously-requested dependencies back together as struct fields.
  def consume_dependencies(item, args=None):
    hydrated_args = args or {}
    for key, value in sorted(item._asdict().items(), key=_key_func):
      if not AddressableDescriptor.is_addressable(item, key):
        hydrated_args[key] = value
        continue

      if isinstance(value, collections.MutableMapping):
        container_type = type(value)
        hydrated_args[key] = container_type((k, maybe_consume(key, v))
                                            for k, v in sorted(value.items(), key=_key_func))
      elif isinstance(value, collections.MutableSequence):
        container_type = type(value)
        hydrated_args[key] = container_type(maybe_consume(key, v) for v in value)
      else:
        hydrated_args[key] = maybe_consume(key, value)
    return _hydrate(type(item), address.spec_path, **hydrated_args)

  return consume_dependencies(struct, args={'address': address})


def _hydrate(item_type, spec_path, **kwargs):
  # If the item will be Locatable, inject the spec_path.
  if issubclass(item_type, Locatable):
    kwargs['spec_path'] = spec_path

  try:
    item = item_type(**kwargs)
  except TypeConstraintError as e:
    raise ResolvedTypeMismatchError(e)

  # Let factories replace the hydrated object.
  if isinstance(item, SerializableFactory):
    item = item.create()

  # Finally make sure objects that can self-validate get a chance to do so.
  if isinstance(item, Validatable):
    item.validate()

  return item


def identity(v):
  return v


def addresses_from_address_family(address_family):
  """Given an AddressFamily, return an Addresses objects containing all of its `addressables`."""
  return Addresses(tuple(address_family.addressables.keys()))


def addresses_from_address_families(address_families):
  """Given a list of AddressFamiliess, return an Addresses object containing all addressables."""
  return Addresses(tuple(a for af in address_families for a in af.addressables.keys()))


def create_graph_tasks(address_mapper, symbol_table_cls):
  """Creates tasks used to parse Structs from BUILD files.

  :param address_mapper: An AddressMapper instance.
  :param symbol_table_cls: A symbol table class.
  """
  return [
    # Support for resolving Structs from Addresses
    (Struct,
      [Select(UnhydratedStruct),
       SelectDependencies(Struct, UnhydratedStruct)],
      hydrate_struct),
    (UnhydratedStruct,
      [SelectProjection(AddressFamily, Path, ('spec_path',), Address),
       Select(Address)],
      resolve_unhydrated_struct),
  ] + [
    # BUILD file parsing.
    (AddressFamily,
      [SelectLiteral(address_mapper, AddressMapper),
       Select(Path),
       SelectProjection(FilesContent, Paths, ('paths',), BuildFilePaths)],
      parse_address_family),
    (BuildFilePaths,
      [SelectLiteral(address_mapper, AddressMapper),
       Select(DirectoryListing)],
      filter_buildfile_paths),
  ] + [
    # Addresses for 'literal' products might possibly be resolvable from BLD files. These tasks
    # define that lookup for each literal product.
    (product,
     [Select(Struct)],
     identity)
    for product in symbol_table_cls.table().values()
  ] + [
    # Spec handling.
    (Addresses,
      [SelectProjection(AddressFamily, Path, ('directory',), SiblingAddresses)],
      addresses_from_address_family),
    (Addresses,
      [SelectDependencies(AddressFamily, RecursiveSubDirectories)],
      addresses_from_address_families),
    # TODO: This is a workaround for the fact that we can't currently "project" in a
    # SelectDependencies clause: we launch the recursion by requesting RecursiveSubDirectories
    # for a Directory projected from DescendantAddresses.
    (RecursiveSubDirectories,
      [SelectProjection(RecursiveSubDirectories, Path, ('directory',), DescendantAddresses)],
      identity),
  ]
