"""A Ren'Py editor (``RENPY_EDIT_PY``) that opens nothing.

When a game launched for an agent crashes, Ren'Py opens ``traceback.txt`` in
the system editor, which puts a window on the user's desktop. RenForge reports
the traceback itself, so launched games get this editor instead.
"""


class Editor(object):
    def begin(self, new_window=False, **kwargs):
        pass

    def open(self, filename, line=None, **kwargs):
        pass

    def end(self, **kwargs):
        pass
