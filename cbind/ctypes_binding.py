# Copyright (C) 2013 Che-Liang Chiou.

'''Parse and generate ctypes binding from C sources with clang.'''

import functools
from cbind.codegen import CodeGen
from cbind.config import SyntaxTreeMatcher
from cbind.passes import (custom_pass,
        scan_required_nodes,
        scan_and_rename,
        scan_forward_decl,
        scan_va_list_tag,
        scan_anonymous_pod)
from cbind.source import SyntaxTreeForest
import cbind.annotations as annotations


HEADER = '''# This is generated by {progname} and should not be edited.

import sys as _python_sys
from ctypes import *

'''


LOAD_LIBRARY = '''
if _python_sys.platform == 'darwin':
    _lib = cdll.LoadLibrary('{darwin_library}')
elif _python_sys.platform == 'win32' or _python_sys.platform == 'cygwin':
    _lib = cdll.LoadLibrary('{windows_library}')
else:
    _lib = cdll.LoadLibrary('{posix_library}')

'''


METHOD_DESCRIPTOR = '''
import types as _python_types

class _CtypesFunctor(object):
    def __init__(self, functor):
        self.functor = functor

    if _python_sys.version_info.major == 3:
        def __get__(self, obj, objtype=None):
            if obj is None:
                return self.functor
            else:
                return _python_types.MethodType(self.functor, obj)

    else:
        def __get__(self, obj, objtype=None):
            return _python_types.MethodType(self.functor, obj, objtype)

'''


class CtypesBindingGenerator:
    '''Generate ctypes binding from C source files with libclang.'''

    def __init__(self):
        '''Initialize the object.'''
        self.codegen = CodeGen()
        self.syntax_tree_forest = SyntaxTreeForest()
        self._config = {}

    def config(self, config_data):
        '''Configure the generator.'''
        if 'preamble' in config_data:
            preamble = config_data['preamble']
            if isinstance(preamble, str):
                self._config['preamble'] = preamble
            else:
                self._config['preamble'] = preamble['codes']
                self._config['library'] = preamble.get('library')
                self._config['use_custom_loader'] = \
                        preamble.get('use_custom_loader')
        for name in 'enum errcheck import method mixin rename'.split():
            if name in config_data:
                matcher = SyntaxTreeMatcher.make(config_data[name])
                self._config[name] = getattr(matcher, 'do_' + name)

    def parse(self, path, contents=None, args=None):
        '''Call parser.parse().'''
        if 'import' in self._config:
            check_required = self._config['import']
        else:
            check_required = functools.partial(check_locally_defined, path=path)

        syntax_tree = self.syntax_tree_forest.parse(path,
                contents=contents, args=args)
        scan_required_nodes(syntax_tree, check_required)
        scan_forward_decl(syntax_tree)
        scan_va_list_tag(syntax_tree)
        scan_anonymous_pod(syntax_tree)

        # Since now tree is "complete", we may attach information to it.
        if 'rename' in self._config:
            scan_and_rename(syntax_tree, self._config['rename'])
        for name in 'enum errcheck method mixin'.split():
            if name in self._config:
                custom_pass(syntax_tree, self._config[name])

    def get_translation_units(self):
        '''Get translation units.'''
        for syntax_tree in self.syntax_tree_forest:
            yield syntax_tree.translation_unit

    def generate_preamble(self, progname, library, output):
        '''Generate preamble of Python binding.'''
        output.write(HEADER.format(progname=progname))
        preamble = self._config.get('preamble', '')
        library = library or self._config.get('library')
        if library:
            if not self._config.get('use_custom_loader'):
                preamble += LOAD_LIBRARY
            library_name = library.partition('.so')[0]
            output.write(preamble.format(
                posix_library=library,
                darwin_library=library_name + '.dylib',
                windows_library=library_name + '.dll'))
        else:
            output.write(preamble)

    def generate(self, output):
        '''Generate ctypes binding.'''
        self.codegen.set_output(output)
        if 'method' in self._config:
            output.write(METHOD_DESCRIPTOR)
        for syntax_tree in self.syntax_tree_forest:
            va_list_tag = syntax_tree.get_annotation(
                    annotations.USE_VA_LIST_TAG, False)
            if va_list_tag:
                self.codegen.generate_record_definition(va_list_tag)
                output.write('\n')
                break
        for syntax_tree in self.syntax_tree_forest:
            syntax_tree.traverse(
                    preorder=self.codegen.generate_record_forward_decl,
                    postorder=self.codegen.generate)


def check_locally_defined(tree, path):
    '''Check if a node is locally defined.'''
    return tree.location.file and tree.location.file.name == path
