#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
from setuptools import setup
extra = {}

try:
    from trac.util.dist import get_l10n_js_cmdclass
    cmdclass = get_l10n_js_cmdclass()
    if cmdclass:
        extra['cmdclass'] = cmdclass

except ImportError:
    pass

setup(**extra)