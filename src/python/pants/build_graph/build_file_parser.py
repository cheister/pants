# coding=utf-8
# Copyright 2014 Pants project contributors (see CONTRIBUTORS.md).
# Licensed under the Apache License, Version 2.0 (see LICENSE).

from __future__ import (absolute_import, division, generators, nested_scopes, print_function,
                        unicode_literals, with_statement)

import logging
import warnings

import six


logger = logging.getLogger(__name__)


# Note: Significant effort has been made to keep the types BuildFile, BuildGraph, Address, and
# Target separated appropriately.  The BuildFileParser is intended to have knowledge of just
# BuildFile and Address.
#
# Here are some guidelines to help maintain this abstraction:
#  - Use the terminology 'address' instead of 'target' in symbols and user messages
#  - Wrap exceptions from BuildFile with a subclass of BuildFileParserError
#     so that callers do not have to reference the BuildFile module
#
# Note: In general, 'spec' should not be a user visible term, it is usually appropriate to
# substitute 'address' instead.
class BuildFileParser(object):
  """Parses BUILD files for a given repo build configuration."""

  class BuildFileParserError(Exception):
    """Base class for all exceptions raised in BuildFileParser to make exception handling easier"""
    pass

  class BuildFileScanError(BuildFileParserError):
    """Raised if there was a problem when gathering all addresses in a BUILD file"""
    pass

  class AddressableConflictException(BuildFileParserError):
    """Raised if the same address is redefined in a BUILD file"""
    pass

  class SiblingConflictException(BuildFileParserError):
    """Raised if the same address is redefined in another BUILD file in the same directory"""
    pass

  class ParseError(BuildFileParserError):
    """An exception was encountered in the python parser"""

  class ExecuteError(BuildFileParserError):
    """An exception was encountered executing code in the BUILD file"""

  def __init__(self, build_configuration, root_dir):
    self._build_configuration = build_configuration
    self._root_dir = root_dir

  @property
  def root_dir(self):
    return self._root_dir

  def registered_aliases(self):
    """Returns a copy of the registered build file aliases this build file parser uses."""
    return self._build_configuration.registered_aliases()

  def address_map_from_build_files(self, build_files):
    family_address_map_by_build_file = self.parse_build_files(build_files)
    address_map = {}
    for build_file, sibling_address_map in family_address_map_by_build_file.items():
      address_map.update(sibling_address_map)
    return address_map

  def parse_build_files(self, build_files):
    family_address_map_by_build_file = {}  # {build_file: {address: addressable}}
    for bf in build_files:
      bf_address_map = self.parse_build_file(bf)
      for address, addressable in bf_address_map.items():
        for sibling_build_file, sibling_address_map in family_address_map_by_build_file.items():
          if address in sibling_address_map:
            raise self.SiblingConflictException(
              "Both {conflicting_file} and {addressable_file} define the same address: "
              "'{target_name}'"
              .format(conflicting_file=sibling_build_file,
                      addressable_file=address.build_file,
                      target_name=address.target_name))
      family_address_map_by_build_file[bf] = bf_address_map
    return family_address_map_by_build_file

  def parse_build_file(self, build_file):
    """Capture Addressable instances from parsing `build_file`.
    Prepare a context for parsing, read a BUILD file from the filesystem, and return the
    Addressable instances generated by executing the code.
    """

    def _format_context_msg(lineno, offset, error_type, message):
      """Show the line of the BUILD file that has the error along with a few line of context"""
      build_contents = build_file.source().decode('utf-8')
      context = "While parsing {build_file}:\n".format(build_file=build_file)
      curr_lineno = 0
      for line in build_contents.split('\n'):
        line = line.encode('ascii', 'backslashreplace')
        curr_lineno += 1
        if curr_lineno == lineno:
          highlight = '*'
        else:
          highlight = ' '
        if curr_lineno >= lineno - 3:
          context += "{highlight}{curr_lineno:4d}: {line}\n".format(
            highlight=highlight, line=line, curr_lineno=curr_lineno)
          if lineno == curr_lineno:
            if offset:
              context += ("       {caret:>{width}} {error_type}: {message}\n\n"
                          .format(caret="^", width=int(offset), error_type=error_type,
                                  message=message))
            else:
              context += ("        {error_type}: {message}\n\n"
                          .format(error_type=error_type, message=message))
        if curr_lineno > lineno + 3:
          break
      return context

    logger.debug("Parsing BUILD file {build_file}."
                 .format(build_file=build_file))

    try:
      build_file_code = build_file.code()
    except SyntaxError as e:
      raise self.ParseError(_format_context_msg(e.lineno, e.offset, e.__class__.__name__, e))
    except Exception as e:
      raise self.ParseError("{error_type}: {message}\n while parsing BUILD file {build_file}"
                            .format(error_type=e.__class__.__name__,
                                    message=e, build_file=build_file))

    parse_state = self._build_configuration.initialize_parse_state(build_file)
    try:
      with warnings.catch_warnings(record=True) as warns:
        six.exec_(build_file_code, parse_state.parse_globals)
        for warn in warns:
          logger.warning(_format_context_msg(lineno=warn.lineno,
                                             offset=None,
                                             error_type=warn.category.__name__,
                                             message=warn.message))
    except Exception as e:
      raise self.ExecuteError("{message}\n while executing BUILD file {build_file}"
                              .format(message=e, build_file=build_file))

    address_map = {}
    for address, addressable in parse_state.registered_addressable_instances:
      logger.debug('Adding {addressable} to the BuildFileParser address map with {address}'
                   .format(addressable=addressable,
                           address=address))
      if address in address_map:
        raise self.AddressableConflictException(
          "File {conflicting_file} defines address '{target_name}' more than once."
          .format(conflicting_file=address.build_file,
                  target_name=address.target_name))
      address_map[address] = addressable

    logger.debug("{build_file} produced the following Addressables:"
                 .format(build_file=build_file))
    for address, addressable in address_map.items():
      logger.debug("  * {address}: {addressable}"
                   .format(address=address,
                           addressable=addressable))
    return address_map
