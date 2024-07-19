# read version from installed package
# from importlib.metadata import version
# __version__ = version("artusplugin")
from trac.util.translation import domain_functions

_, tag_, N_, add_domain = \
    domain_functions('artusplugin', ('_', 'tag_', 'N_', 'add_domain'))
