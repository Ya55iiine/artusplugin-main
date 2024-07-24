import six
import re
from trac.util.html import Markup
from six.moves import html_entities

def striptags(html):
    return re.compile(r'(<!--.*?-->|<[^>]*>)').sub('', html)

def stripentities(text):
    def _replace_entity(match):
        if match.group(1): # numeric entity
            ref = match.group(1)
            if ref.startswith('x'):
                ref = int(ref[1:], 16)
            else:
                ref = int(ref, 10)
            return six.unichr(ref)
        else: # character entity
            ref = match.group(2)
            try:
                return six.unichr(html_entities.name2codepoint[ref])
            except KeyError:
                return ref
    return re.compile(r'&(?:#((?:\d+)|(?:[xX][0-9a-fA-F]+));?|(\w+);)').sub(_replace_entity, text)

def plaintext(text):
    return stripentities(striptags(text))

class TextSerializer(object):
    def __init__(self, strip_markup=False):
        self.strip_markup = strip_markup

    def __call__(self, stream):
        strip_markup = self.strip_markup
        for event in stream:
            if event[0] is TEXT:
                data = event[1]
                if strip_markup and type(data) is Markup:
                    data = data.striptags().stripentities()
                yield six.text_type(data)
                
class StreamEventKind(str):
    """A kind of event on a markup stream."""
    __slots__ = []
    _instances = {}

    def __new__(cls, val):
        return cls._instances.setdefault(val, str.__new__(cls, val))
    
class Stream:
    TEXT = StreamEventKind('TEXT')
    
TEXT = Stream.TEXT

