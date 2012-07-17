
import logging

import wx

LOG_LINES = 500
log = None
_level = logging.DEBUG


def get_logger():
    logging.basicConfig(format=" - %(levelname)s \t%(message)s")
    l = logging.getLogger()
    l.setLevel(_level)
    l.handlers[0].setFormatter(
      logging.Formatter("%(asctime)s [%(levelname)s] %(message)s", "%H:%M:%S"))

    return l

def create_gui_logger(log_field):
    gui_format = logging.Formatter('%(asctime)s - %(message)s', '%H:%M:%S')
    text_field_handler = TextFieldHandler()
    text_field_handler.setTextField(log_field)
    text_field_handler.setFormatter(gui_format)
    log.debug("Switching to GUI logger")
    log.addHandler(text_field_handler)

    for handler in log.handlers:
        if not isinstance(handler, TextFieldHandler):
            log.removeHandler(handler)

class TextFieldHandler(logging.Handler):
    """` Custom log handler, used to output log entries to a text field. """
    def __init__(self):
        """ Call the parent constructor and initialize the handler """
        logging.Handler.__init__(self)
        self.textfield = None

    def setTextField(self, textfield):
        self.textfield = textfield
        self.textfield.Clear()

    def emit(self, record):
        """ Write a record, in color, to a text field. """
        if self.textfield is not None:
            if self.textfield.GetNumberOfLines() > LOG_LINES:
                # Removes the characters from posit`ion 0 up to and including the first line break
                self.textfield.Remove(0, self.textfield.GetValue().find('\n') + 1)
                #self.textfield.Remove(self.textfield.GetValue().rfind('\n'), len(self.textfield.GetValue()))

            color = "#777777"

            if record.levelno > logging.WARNING:
                color = "#B00B2C"
            elif record.levelno > logging.INFO:
                color = "#C87000"
            elif record.levelno > logging.DEBUG:
                color = "#555555"
            else:
                color = "#777777"

            # Do the actual writing in a CallAfter, so logging won't interfere
            # with the GUI drawing process.
            wx.CallAfter(self.write_to_field, record, color)

    def write_to_field(self, record, color):

        if self.textfield.GetNumberOfLines() > LOG_LINES:
            # Removes the characters from posit`ion 0 up to and including the first line break
            self.textfield.Remove(0, self.textfield.GetValue().find('\n') + 1)
            #self.textfield.Remove(self.textfield.GetValue().rfind('\n'), len(self.textfield.GetValue()))

        self.textfield.SetDefaultStyle(wx.TextAttr(color, None))
        self.textfield.AppendText(''.join([self.format(record), '\n']))
        self.textfield.LineUp()



if log is None:
    log = get_logger()
