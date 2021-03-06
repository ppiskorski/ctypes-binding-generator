# Copyright (C) 2013 Che-Liang Chiou.

'''Scan syntax tree's use of __va_list_tag.'''

from cbind.passes.util import traverse_postorder, strip_type
import cbind.annotations as annotations


def scan_va_list_tag(syntax_tree):
    '''Scan use of __va_list_tag.'''
    try:
        postorder = lambda tree: _scan_tree(tree, syntax_tree)
        traverse_postorder(syntax_tree, postorder)
    except StopIteration:
        pass


def _scan_tree(tree, root):
    '''Scan this tree for __va_list_tag.'''
    if not tree.get_annotation(annotations.REQUIRED, False):
        return
    type_ = tree.type
    while True:
        if tree.name == '__va_list_tag':
            root.annotate(annotations.USE_VA_LIST_TAG, tree)
            raise StopIteration()  # Return from deeply recursion
        while True:
            type_ = strip_type(type_)
            if not type_:
                return
            if type_.is_user_defined_type():
                tree = type_.get_declaration()
                break
