from __future__ import unicode_literals, print_function

from fs.path import pathjoin, basename
from fs.errors import FSError

import mimetypes
import json


class ReaderError(Exception):
    pass


class UnknownFormat(ReaderError):
    """Attempt to read a format we don't understand"""


class RelativePathError(ReaderError):
    """Can't read from a relative with without an app"""


class DataReader(object):
    """Reads and decodes files of known types"""

    def __init__(self, fs):
        self.fs = fs

    def __repr__(self):
        return "<reader {!r}>".format(self.fs)

    def read(self, path, app=None, mime_type=None):
        """Read a file"""
        if not path.startswith('/'):
            if app is None:
                raise RelativePathError("Can't use relative data paths with an application")
            path = pathjoin(app.data_directory, path)

        filename = basename(path)
        if mime_type is None:
            mime_type, encoding = mimetypes.guess_type(filename)

        _type, sub_type = mime_type.split('/', 1)

        if mime_type == "text/plain":
            data = self.fs.getcontents(path, mode="rt", encoding="utf-8")
        elif mime_type == "application/json":
            with self.fs.open(path, 'rt', encoding="utf-8") as f:
                data = json.load(f)
        elif mime_type == "application/octet-stream":
            data = self.fs.getcontents(path, mode="rb")

        elif _type == "text":
            data = self.fs.getcontents(path, mode="rt", encoding="utf-8")

        else:
            raise UnknownFormat("Moya doesn't know how to read file '{}' (in {!r})".format(path, self.fs))

        return data

    def exists(self, path, app):
        """Check if a file exists"""
        if not path.startswith('/'):
            if app is None:
                raise RelativePathError("Can't use relative data paths with an application")
            path = pathjoin(app.data_directory, path)
        try:
            return self.fs.isfile(path)
        except FSError:
            return False


if __name__ == "__main__":

    reader = DataReader(None)
    reader.read('test.bin')
