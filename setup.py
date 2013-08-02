#!/usr/bin/env python

from distutils.core import setup

setup(
    name='ctypes-binding-generator',
    version='0.2.0',
    description='Generate ctypes binding from C source files',
    author='Che-Liang Chiou',
    author_email='clchiou@gmail.com',
    packages=['cbind', 'cbind/passes'],
    scripts=['bin/cbind'],
)
