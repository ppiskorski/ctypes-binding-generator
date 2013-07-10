'''Parse and generate ctypes binding from C sources with clang.'''

from clang.cindex import Index, CursorKind, TypeKind, conf


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
        TypeKind.VECTOR:            None,
}

# Indent by 4 speces
INDENT = '    '

POD_DECL = frozenset((CursorKind.STRUCT_DECL, CursorKind.UNION_DECL))

BLOB_TYPE = frozenset((TypeKind.UNEXPOSED, TypeKind.RECORD))


class CtypesBindingException(Exception):
    '''Exception raised by CtypesBindingGenerator class.'''
    pass


class CParser:
    '''Parse C source files with libclang.'''

    def __init__(self):
        '''Initialize the object.'''
        self._index = Index.create()
        self.symbol_table = SymbolTable()
        self.forward_declaration = SymbolTable()
        self.translation_units = []
        self._this_file = None
        self._foreigners = None

    def parse(self, path, contents, args):
        '''Parse C source file.'''
        if contents:
            unsaved_files = [(path, contents)]
        else:
            unsaved_files = None
        translation_unit = self._index.parse(path, args=args,
                unsaved_files=unsaved_files)
        if not translation_unit:
            msg = 'Could not parse C source: %s' % path
            raise CtypesBindingException(msg)
        self.translation_units.append(translation_unit)

        # We run through the AST for two passes:
        # * The first pass (right below) enumerates the symbols that we should
        #   generate Python codes for.
        # * The second pass in the generate() method generates the codes.
        self._this_file = path
        self._foreigners = []
        walk_astree(translation_unit.cursor,
                None, lambda cursor: self._extract_symbol(cursor, path))
        # Foreigners are symbols not defined locally in this file.
        while self._foreigners:
            foreigners = self._foreigners
            self._foreigners = []
            for cursor in foreigners:
                self._extract_symbol(cursor, cursor.location.file.name)

    def traverse(self, preorder, postorder):
        '''Traverse ASTs.'''
        for tunit in self.translation_units:
            walk_astree(tunit.cursor, preorder, postorder)

    def _extract_symbol(self, cursor, c_src):
        '''Extract symbols that we should generate Python codes for.'''
        # Ignore this node if it does not belong to the C source.
        if not cursor.location.file or cursor.location.file.name != c_src:
            return
        # Do not extract this node twice.
        if cursor in self.symbol_table:
            return

        # Add this node to the symbol table before searching for nodes that
        # this node depends on to avoid infinite recursion caused by cyclic
        # reference.
        if cursor.kind is CursorKind.TYPEDEF_DECL:
            self.symbol_table.add(cursor)
        elif cursor.kind is CursorKind.FUNCTION_DECL:
            self.symbol_table.add(cursor)
            for type_ in cursor.type.argument_types():
                self._extract_type(type_)
            self._extract_type(cursor.result_type)
        elif cursor.kind in POD_DECL and cursor.is_definition():
            for field in cursor.get_children():
                if field.kind is CursorKind.FIELD_DECL:
                    self._extract_type(field.type)
                elif field.kind in POD_DECL:
                    self._extract_symbol(field, c_src)
            self.symbol_table.add(cursor)
        elif cursor.kind is CursorKind.ENUM_DECL and cursor.is_definition():
            self.symbol_table.add(cursor)
        elif cursor.kind is CursorKind.VAR_DECL:
            self.symbol_table.add(cursor)
            self._extract_type(cursor.type)
        else:
            return

    def _extract_type(self, type_):
        '''Extract symbols from this clang type.'''
        if type_.kind in BLOB_TYPE:
            cursor = type_.get_declaration()
            if cursor.location.file.name != self._this_file:
                self._foreigners.append(cursor)
            elif (cursor.kind in POD_DECL and
                    cursor not in self.symbol_table):
                self.forward_declaration.add(cursor)
        elif type_.kind is TypeKind.TYPEDEF:
            self._extract_type(type_.get_canonical())
        elif type_.kind is TypeKind.CONSTANTARRAY:
            self._extract_type(type_.get_array_element_type())
        elif type_.kind is TypeKind.POINTER:
            self._extract_type(type_.get_pointee())


class CtypesBindingGenerator:
    '''Generate ctypes binding from C source files with libclang.'''

    def __init__(self, libvar=None):
        '''Initialize the object.'''
        self.parser = CParser()
        self.libvar = libvar or '_lib'
        self.anonymous_serial = 0
        # For convenience...
        self.symbol_table = self.parser.symbol_table

    def parse(self, path, contents=None, args=None):
        '''Call parser.parse().'''
        self.parser.parse(path, contents, args)

    def generate(self, output):
        '''Generate ctypes binding.'''
        preorder = lambda cursor: self._make_forward_decl(cursor, output)
        postorder = lambda cursor: self._make(cursor, output)
        self.parser.traverse(preorder, postorder)

    def _make_forward_decl(self, cursor, output):
        '''Generate forward declaration for nodes.'''
        if cursor not in self.parser.forward_declaration:
            return
        declared = self.symbol_table.get_annotation(cursor, 'declared', False)
        self._make_pod(cursor, output, declared=declared, declaration=True)
        self.symbol_table.annotate(cursor, 'declared', True)

    def _make(self, cursor, output):
        '''Generate ctypes binding from a AST node.'''
        # Do not process node that is not in the symbol table.
        if cursor not in self.symbol_table:
            return
        # Do not define a node twice.
        if self.symbol_table.get_annotation(cursor, 'defined', False):
            return
        # TODO(clchiou): Function pointer.
        declaration = False
        if cursor.kind is CursorKind.TYPEDEF_DECL:
            self._make_typedef(cursor, output)
        elif cursor.kind is CursorKind.FUNCTION_DECL:
            self._make_function(cursor, output)
        elif cursor.kind in POD_DECL:
            declared = self.symbol_table.get_annotation(cursor,
                    'declared', False)
            declaration = not cursor.is_definition()
            self._make_pod(cursor, output,
                    declared=declared, declaration=declaration)
        elif cursor.kind is CursorKind.ENUM_DECL and cursor.is_definition():
            self._make_enum(cursor, output)
        elif cursor.kind is CursorKind.VAR_DECL:
            self._make_var(cursor, output)
        else:
            return
        output.write('\n')
        if declaration:
            self.symbol_table.annotate(cursor, 'declared', True)
        else:
            self.symbol_table.annotate(cursor, 'defined', True)

    def _make_type(self, type_):
        '''Generate ctypes binding of a clang type.'''
        c_type = None
        if type_.kind in BLOB_TYPE:
            cursor = type_.get_declaration()
            if cursor.spelling:
                c_type = cursor.spelling
            elif cursor.kind is CursorKind.ENUM_DECL:
                c_type = self._make_type(cursor.enum_type)
            else:
                c_type = self.symbol_table.get_annotation(cursor, 'name')
        elif type_.kind is TypeKind.TYPEDEF:
            c_type = self._make_type(type_.get_canonical())
        elif type_.kind is TypeKind.CONSTANTARRAY:
            element_type = self._make_type(type_.get_array_element_type())
            c_type = '%s * %d' % (element_type, type_.get_array_size())
        elif type_.kind is TypeKind.POINTER:
            c_type = self._make_pointer_type(type_)
        else:
            c_type = C_TYPE_MAP.get(type_.kind)
        if c_type is None:
            raise TypeError('Unsupported TypeKind: %s' % type_.kind)
        return c_type

    def _make_pointer_type(self, type_):
        '''Generate ctypes binding of a pointer.'''
        pointee_type = type_.get_pointee()
        if pointee_type.kind is TypeKind.CHAR_S:
            return 'c_char_p'
        elif pointee_type.kind is TypeKind.WCHAR:
            return 'c_wchar_p'
        elif pointee_type.kind is TypeKind.VOID:
            return 'c_void_p'
        elif (pointee_type.kind is TypeKind.TYPEDEF and
                pointee_type.get_canonical().kind is TypeKind.VOID):
            # Handle special case "typedef void foo;"
            return 'c_void_p'
        else:
            return 'POINTER(%s)' % self._make_type(pointee_type)

    def _make_typedef(self, cursor, output):
        '''Generate ctypes binding of a typedef statement.'''
        type_ = cursor.underlying_typedef_type
        # Handle special case "typedef void foo;"
        if type_.kind is TypeKind.VOID:
            return
        output.write('%s = %s\n' % (cursor.spelling, self._make_type(type_)))

    def _make_function(self, cursor, output):
        '''Generate ctypes binding of a function declaration.'''
        name = cursor.spelling
        output.write('{0} = {1}.{0}\n'.format(name, self.libvar))
        if conf.lib.clang_Cursor_getNumArguments(cursor):
            argtypes = '[%s]' % ', '.join(self._make_type(type_)
                    for type_ in cursor.type.argument_types())
            output.write('%s.argtypes = %s\n' % (name, argtypes))
        if cursor.result_type.kind is not TypeKind.VOID:
            restype = self._make_type(cursor.result_type)
            output.write('%s.restype = %s\n' % (name, restype))

    def _make_pod(self, cursor, output, declared=False, declaration=False):
        '''Generate ctypes binding of a POD definition.'''
        name = self._make_pod_name(cursor)
        output_header = not declared
        output_body = not declaration
        if output_header:
            self._make_pod_header(cursor, name, output_body, output)
        if output_body:
            self._make_pod_body(cursor, name, output_header, output)

    def _make_pod_name(self, cursor):
        '''Generate the name of the POD.'''
        if cursor.spelling:
            name = cursor.spelling
        else:
            if cursor.kind is CursorKind.STRUCT_DECL:
                name = '_anonymous_struct_%04d'
            else:
                name = '_anonymous_union_%04d'
            name = name % self._next_anonymous_serial()
            self.symbol_table.annotate(cursor, 'name', name)
        return name

    @staticmethod
    def _make_pod_header(cursor, name, output_body, output):
        '''Generate the 'class ...' part of POD.'''
        if cursor.kind is CursorKind.STRUCT_DECL:
            pod_kind = 'Structure'
        else:
            pod_kind = 'Union'
        output.write('class {0}({1}):\n'.format(name, pod_kind))
        if not output_body:
            output.write('%spass\n' % INDENT)

    def _make_pod_body(self, cursor, name, output_header, output):
        '''Generate the body part of POD.'''
        fields = [field for field in cursor.get_children()
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
                (begin, cursor.type.get_align()))
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
        cursor = field.type.get_declaration()
        return not bool(cursor.spelling)

    def _make_enum(self, cursor, output):
        '''Generate ctypes binding of a enum definition.'''
        if cursor.spelling:
            output.write('%s = %s\n' %
                    (cursor.spelling, self._make_type(cursor.enum_type)))
            c_type = cursor.spelling
        else:
            c_type = self._make_type(cursor.enum_type)
        for enum in cursor.get_children():
            output.write('%s = %s(%s)\n' %
                    (enum.spelling, c_type, enum.enum_value))

    def _make_var(self, cursor, output):
        '''Generate ctypes binding of a variable declaration.'''
        name = cursor.spelling
        c_type = self._make_type(cursor.type)
        output.write('{0} = {1}.in_dll({2}, \'{0}\')\n'.
                format(name, c_type, self.libvar))

    def _next_anonymous_serial(self):
        '''Generate a serial number for anonymous stuff.'''
        self.anonymous_serial += 1
        return self.anonymous_serial


class SymbolTable:
    '''Table of AST nodes.  This table may store nodes as well as annotations
    of nodes; this feature is useful for recording the names generated for
    anonymous POD (struct or union).
    '''

    @staticmethod
    def _hash_node(node):
        '''Compute the hash-key of the node.'''
        if node.spelling:
            return '%s:%s' % (node.kind, node.spelling)
        if node.location.file:
            filename = node.location.file.name
        else:
            filename = '?'
        return '%s:%s:%d' % (node.kind, filename, node.location.offset)

    def __init__(self):
        '''Initialize an empty SymbolTable.'''
        self._table = {}

    def __contains__(self, node):
        '''Return true if node is in the table.'''
        node_key = self._hash_node(node)
        return node_key in self._table

    def add(self, node):
        '''Store node as a pair of the node and its annotations.'''
        node_key = self._hash_node(node)
        if node_key in self._table:
            return
        self._table[node_key] = (node, {})

    def annotate(self, node, key, value):
        '''Annotate a node with (key, value).'''
        node_key = self._hash_node(node)
        annotations = self._table[node_key][1]
        annotations[key] = value

    def get_annotation(self, node, key, default=None):
        '''Get the annotation value of key of the node.'''
        node_key = self._hash_node(node)
        annotations = self._table[node_key][1]
        if default is None:
            return annotations[key]
        else:
            return annotations.get(key, default)


def walk_astree(cursor, preorder, postorder):
    '''Recursively walk through the AST.'''
    if preorder:
        preorder(cursor)
    for child in cursor.get_children():
        walk_astree(child, preorder, postorder)
    if postorder:
        postorder(cursor)
