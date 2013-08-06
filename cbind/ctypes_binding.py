'''Parse and generate ctypes binding from C sources with clang.'''

import logging
from clang.cindex import CursorKind, TypeKind
from cbind.source import SyntaxTree
from cbind.passes import (scan_required_nodes, scan_forward_decl,
        scan_va_list_tag, scan_typedef_pod)
import cbind.annotations as annotations


# Map of clang type to ctypes type
C_TYPE_MAP = {
        TypeKind.INVALID:           None,
        TypeKind.UNEXPOSED:         None,
        TypeKind.VOID:              None,
        TypeKind.BOOL:              'c_bool',
        TypeKind.CHAR_U:            'c_ubyte',
        TypeKind.UCHAR:             'c_ubyte',
        TypeKind.CHAR16:            None,
        TypeKind.CHAR32:            None,
        TypeKind.USHORT:            'c_ushort',
        TypeKind.UINT:              'c_uint',
        TypeKind.ULONG:             'c_ulong',
        TypeKind.ULONGLONG:         'c_ulonglong',
        TypeKind.UINT128:           None,
        TypeKind.CHAR_S:            'c_char',
        TypeKind.SCHAR:             'c_char',
        TypeKind.WCHAR:             'c_wchar',
        TypeKind.SHORT:             'c_short',
        TypeKind.INT:               'c_int',
        TypeKind.LONG:              'c_long',
        TypeKind.LONGLONG:          'c_longlong',
        TypeKind.INT128:            None,
        TypeKind.FLOAT:             'c_float',
        TypeKind.DOUBLE:            'c_double',
        TypeKind.LONGDOUBLE:        'c_longdouble',
        TypeKind.NULLPTR:           None,
        TypeKind.OVERLOAD:          None,
        TypeKind.DEPENDENT:         None,
        TypeKind.OBJCID:            None,
        TypeKind.OBJCCLASS:         None,
        TypeKind.OBJCSEL:           None,
        TypeKind.COMPLEX:           None,
        TypeKind.POINTER:           None,
        TypeKind.BLOCKPOINTER:      None,
        TypeKind.LVALUEREFERENCE:   None,
        TypeKind.RVALUEREFERENCE:   None,
        TypeKind.RECORD:            None,
        TypeKind.ENUM:              None,
        TypeKind.TYPEDEF:           None,
        TypeKind.OBJCINTERFACE:     None,
        TypeKind.OBJCOBJECTPOINTER: None,
        TypeKind.FUNCTIONNOPROTO:   None,
        TypeKind.FUNCTIONPROTO:     None,
        TypeKind.CONSTANTARRAY:     None,
        TypeKind.INCOMPLETEARRAY:   None,
        TypeKind.VARIABLEARRAY:     None,
        TypeKind.DEPENDENTSIZEDARRAY: None,
        TypeKind.VECTOR:            None,
}

# Typedef'ed types of stddef.h, etc.
BUILTIN_TYPEDEFS = {
        'size_t': 'c_size_t',
        'ssize_t': 'c_ssize_t',
        'wchar_t': 'c_wchar_t',
}

# Indent by 4 speces
INDENT = '    '

# Name of the library
LIBNAME = '_lib'


class CtypesBindingGenerator:
    '''Generate ctypes binding from C source files with libclang.'''

    def __init__(self):
        '''Initialize the object.'''
        self.syntax_trees = []
        self.anonymous_serial = 0

    def parse(self, path, contents=None, args=None):
        '''Call parser.parse().'''
        syntax_tree = SyntaxTree.parse(path, contents=contents, args=args)
        scan_required_nodes(syntax_tree, path)
        scan_forward_decl(syntax_tree)
        scan_va_list_tag(syntax_tree)
        scan_typedef_pod(syntax_tree)
        self.syntax_trees.append(syntax_tree)

    def get_translation_units(self):
        '''Get translation units.'''
        for syntax_tree in self.syntax_trees:
            yield syntax_tree.translation_unit

    def generate(self, output):
        '''Generate ctypes binding.'''
        for syntax_tree in self.syntax_trees:
            va_list_tag = syntax_tree.get_annotation(
                    annotations.USE_VA_LIST_TAG, False)
            if va_list_tag:
                self._make_pod(va_list_tag, output,
                        declared=False, declaration=False)
                output.write('\n')
                break
        preorder = lambda tree: self._make_forward_decl(tree, output)
        postorder = lambda tree: self._make(tree, output)
        for syntax_tree in self.syntax_trees:
            syntax_tree.traverse(preorder=preorder, postorder=postorder)

    def _make_forward_decl(self, tree, output):
        '''Generate forward declaration for nodes.'''
        if not tree.get_annotation(annotations.REQUIRED, False):
            return
        if not tree.get_annotation(annotations.FORWARD_DECLARATION, False):
            return
        declared = tree.get_annotation(annotations.DECLARED, False)
        self._make_pod(tree, output, declared=declared, declaration=True)
        tree.annotate(annotations.DECLARED, True)

    def _make(self, tree, output):
        '''Generate ctypes binding from a AST node.'''
        if not tree.get_annotation(annotations.REQUIRED, False):
            return
        # Do not define a node twice.
        if tree.get_annotation(annotations.DEFINED, False):
            return
        declaration = False
        if tree.kind is CursorKind.TYPEDEF_DECL:
            self._make_typedef(tree, output)
        elif tree.kind is CursorKind.FUNCTION_DECL:
            self._make_function(tree, output)
        elif (tree.kind is CursorKind.STRUCT_DECL or
                tree.kind is CursorKind.UNION_DECL):
            declared = tree.get_annotation(annotations.DECLARED, False)
            declaration = not tree.is_definition()
            self._make_pod(tree, output,
                    declared=declared, declaration=declaration)
        elif tree.kind is CursorKind.ENUM_DECL and tree.is_definition():
            self._make_enum(tree, output)
        elif tree.kind is CursorKind.VAR_DECL:
            self._make_var(tree, output)
        else:
            return
        output.write('\n')
        if declaration:
            tree.annotate(annotations.DECLARED, True)
        else:
            tree.annotate(annotations.DEFINED, True)

    def _make_type(self, type_):
        '''Generate ctypes binding of a clang type.'''
        c_type = None
        if type_.is_user_defined_type():
            tree = type_.get_declaration()
            if tree.spelling:
                c_type = tree.spelling
            elif tree.kind is CursorKind.ENUM_DECL:
                c_type = self._make_type(tree.enum_type)
            else:
                c_type = tree.get_annotation(annotations.NAME)
        elif type_.kind is TypeKind.TYPEDEF:
            tree = type_.get_declaration()
            c_type = (BUILTIN_TYPEDEFS.get(tree.spelling) or
                    self._make_type(type_.get_canonical()))
        elif type_.kind is TypeKind.CONSTANTARRAY:
            # TODO(clchiou): Make parentheses context-sensitive
            element_type = self._make_type(type_.get_array_element_type())
            c_type = '(%s * %d)' % (element_type, type_.get_array_size())
        elif type_.kind is TypeKind.INCOMPLETEARRAY:
            pointee_type = type_.get_array_element_type()
            c_type = self._make_pointer_type(pointee_type=pointee_type)
        elif type_.kind is TypeKind.POINTER:
            c_type = self._make_pointer_type(pointer_type=type_)
        else:
            c_type = C_TYPE_MAP.get(type_.kind)
        if c_type is None:
            raise TypeError('Unsupported TypeKind: %s' % type_.kind)
        return c_type

    def _make_pointer_type(self, pointer_type=None, pointee_type=None):
        '''Generate ctypes binding of a pointer.'''
        if pointer_type:
            pointee_type = pointer_type.get_pointee()
        canonical = pointee_type.get_canonical()
        decl = pointee_type.get_declaration()
        if pointee_type.kind is TypeKind.CHAR_S:
            c_type = 'c_char_p'
        elif pointee_type.kind is TypeKind.WCHAR:
            c_type = 'c_wchar_p'
        elif pointee_type.kind is TypeKind.VOID:
            c_type = 'c_void_p'
        elif (pointee_type.kind is TypeKind.TYPEDEF and
                canonical.kind is TypeKind.VOID):
            # Handle special case "typedef void foo;"
            c_type = 'c_void_p'
        elif (pointee_type.kind is TypeKind.TYPEDEF and
                decl.spelling == 'wchar_t'):
            c_type = 'c_wchar_p'
        elif canonical.kind is TypeKind.FUNCTIONPROTO:
            c_type = self._make_function_pointer(canonical)
        else:
            c_type = 'POINTER(%s)' % self._make_type(pointee_type)
        return c_type

    def _make_function_pointer(self, type_):
        '''Generate ctypes binding of a function pointer.'''
        # ctypes does not support variadic function pointer...
        if type_.is_function_variadic():
            logging.info('Could not generate pointer to variadic function')
            return 'c_void_p'
        args = type_.get_argument_types()
        if len(args) > 0:
            argtypes = ', %s' % ', '.join(self._make_type(arg) for arg in args)
        else:
            argtypes = ''
        result_type = type_.get_result()
        if result_type.kind is TypeKind.VOID:
            restype = 'None'
        else:
            restype = self._make_type(result_type)
        return 'CFUNCTYPE(%s%s)' % (restype, argtypes)

    def _make_typedef(self, tree, output):
        '''Generate ctypes binding of a typedef statement.'''
        type_ = tree.underlying_typedef_type
        # Handle special case "typedef void foo;"
        if type_.kind is TypeKind.VOID:
            return
        output.write('%s = %s\n' % (tree.spelling, self._make_type(type_)))

    def _make_function(self, tree, output):
        '''Generate ctypes binding of a function declaration.'''
        if not tree.is_external_linkage():
            return
        name = tree.spelling
        output.write('{0} = {1}.{0}\n'.format(name, LIBNAME))
        argtypes = self._make_function_arguments(tree)
        if argtypes:
            output.write('%s.argtypes = [%s]\n' % (name, argtypes))
        if tree.result_type.kind is not TypeKind.VOID:
            restype = self._make_type(tree.result_type)
            output.write('%s.restype = %s\n' % (name, restype))

    def _make_function_arguments(self, tree):
        '''Generate ctypes binding of function's arguments.'''
        if tree.type.is_function_variadic() or tree.num_arguments <= 0:
            return None
        args = (self._make_type(arg.type) for arg in tree.get_arguments())
        return ', '.join(args)

    def _make_pod(self, tree, output, declared=False, declaration=False):
        '''Generate ctypes binding of a POD definition.'''
        name = self._make_pod_name(tree)
        output_header = not declared
        output_body = not declaration
        if output_header:
            self._make_pod_header(tree, name, output_body, output)
        if output_body:
            self._make_pod_body(tree, name, output_header, output)

    def _make_pod_name(self, tree):
        '''Generate the name of the POD.'''
        if tree.spelling:
            return tree.spelling
        name = tree.get_annotation(annotations.NAME, False)
        if not name:
            if tree.kind is CursorKind.STRUCT_DECL:
                name = '_anonymous_struct_%04d'
            else:
                name = '_anonymous_union_%04d'
            name = name % self._next_anonymous_serial()
            tree.annotate(annotations.NAME, name)
        return name

    @staticmethod
    def _make_pod_header(tree, name, output_body, output):
        '''Generate the 'class ...' part of POD.'''
        if tree.kind is CursorKind.STRUCT_DECL:
            pod_kind = 'Structure'
        else:
            pod_kind = 'Union'
        output.write('class {0}({1}):\n'.format(name, pod_kind))
        if not output_body:
            output.write('%spass\n' % INDENT)

    def _make_pod_body(self, tree, name, output_header, output):
        '''Generate the body part of POD.'''
        fields = [field for field in tree.get_children()
                if field.kind is CursorKind.FIELD_DECL]
        if not fields:
            if output_header:
                output.write('%spass\n' % INDENT)
            return
        if output_header:
            begin = INDENT
        else:
            begin = '%s.' % name
        # Generate _anonymous_
        anonymous = []
        for field in fields:
            if self._is_pod_field_anonymous(field):
                anonymous.append('\'%s\'' % field.spelling)
        if len(anonymous) == 1:
            output.write('%s_anonymous_ = (%s,)\n' %
                    (begin, anonymous[0]))
        elif anonymous:
            output.write('%s_anonymous_ = (%s)\n' %
                    (begin, ', '.join(anonymous)))
        # Generate _pack_
        output.write('%s_pack_ = %d\n' %
                (begin, tree.type.get_align()))
        # Generate _fields_
        self._make_pod_fields(begin, fields, output)

    def _make_pod_fields(self, begin, fields, output):
        '''Generate ctypes _field_ statement.'''
        field_stmt = '%s_fields_ = [' % begin
        indent = ' ' * len(field_stmt)
        output.write(field_stmt)
        first = True
        for field in fields:
            blob = ['\'%s\'' % field.spelling, self._make_type(field.type)]
            if field.is_bitfield():
                blob.append(str(field.get_bitfield_width()))
            field_stmt = '(%s)' % ', '.join(blob)
            if first:
                first = False
            else:
                output.write(',\n%s' % indent)
            output.write('%s' % field_stmt)
        output.write(']\n')

    @staticmethod
    def _is_pod_field_anonymous(field):
        '''Test if this field is an anonymous one.'''
        if field.type.kind is not TypeKind.UNEXPOSED:
            return False
        tree = field.type.get_declaration()
        return not bool(tree.spelling)

    def _make_enum(self, tree, output):
        '''Generate ctypes binding of a enum definition.'''
        if tree.spelling:
            output.write('%s = %s\n' %
                    (tree.spelling, self._make_type(tree.enum_type)))
        for enum in tree.get_children():
            output.write('%s = %s\n' % (enum.spelling, enum.enum_value))

    def _make_var(self, tree, output):
        '''Generate ctypes binding of a variable declaration.'''
        name = tree.spelling
        c_type = self._make_type(tree.type)
        output.write('{0} = {1}.in_dll({2}, \'{0}\')\n'.
                format(name, c_type, LIBNAME))

    def _next_anonymous_serial(self):
        '''Generate a serial number for anonymous stuff.'''
        self.anonymous_serial += 1
        return self.anonymous_serial
