import curses
import os
import time
import locale

from .file import FileChunker
from .packet import Packet
from .parser import Parser
from .printer import PrinterANSI


def setstyle(self, fg=None, bg=None):
    return '\033[' + chr(fg or self.fg) + chr(bg or self.bg) + chr(1 if self.flash else 0 + 2 if self.conceal else 0)


PrinterANSI.setstyle = setstyle


class TerminalTooSmall(Exception):
    pass


class ParserNcurses(Parser):
    def __init__(self, tt, scr, row):
        self._scr = scr
        self._row = row
        super().__init__(tt)

    def emitcharacter(self, c):
        colour = Interactive.colours[self._state['fg']] | Interactive.colours[self._state['bg']] << 3
        if self._state['conceal']:
            colour += 64
        self._scr.addstr(self._row, self._pos, c, curses.color_pair(colour+1) | (curses.A_BLINK if self._state['flash'] else 0))
        self._pos += 1

    def parse(self):
        self._pos = 0 if self._row else 8
        super().parse()


class Interactive(object):
    colours = {0: curses.COLOR_BLACK, 1: curses.COLOR_RED, 2: curses.COLOR_GREEN, 3: curses.COLOR_YELLOW,
               4: curses.COLOR_BLUE, 5: curses.COLOR_MAGENTA, 6: curses.COLOR_CYAN, 7: curses.COLOR_WHITE}

    def __init__(self, packet_iter, scr, initial_page=0x100):
        self.scr = scr

        self.packet_iter = packet_iter

        if initial_page is None:
            self.magazine = 1
            self.page = 0
        else:
            self.magazine = initial_page >> 8
            self.page = initial_page & 0xff
        self.last_subpage = None
        self.last_header = None
        self.inputtmp = [None, None, None]
        self.inputstate = 0
        self.need_clear = False
        self.hold = False
        self.reveal = False
        self.links = [(7, 255), (7, 255), (7, 255), (7, 255)]

        y, x = self.scr.getmaxyx()
        if x < 41 or y < 25:
            raise TerminalTooSmall(x, y)

        curses.start_color()
        for n in range(64):
            curses.init_pair(n + 1, Interactive.colours[n & 0x7], Interactive.colours[n >> 3])
        self.set_concealed_pairs()

        self.scr.nodelay(1)
        curses.curs_set(0)

        self.set_input_field('P%d%02x' % (self.magazine, self.page))

    def set_concealed_pairs(self, show=False):
        for n in range(16):
            # workaround for ncurses bug where pairs are only refreshed if their previous
            # fg and bg are both not black. one of the temp colours here must be not black
            # and one must be different to the actual desired colours.
            curses.init_pair(n + 1 + 64, 0, 1)
        for n in range(64):
            curses.init_pair(n + 1 + 64, Interactive.colours[n & 0x7] if show else Interactive.colours[n >> 3],
                             Interactive.colours[n >> 3])

    def set_input_field(self, str, clr=0):
        self.scr.addstr(0, 3, str, curses.color_pair(clr))

    def go_page(self, magazine, page):
        self.inputstate = 0
        self.magazine = magazine
        self.page = page
        self.set_input_field('P%d%02x' % (self.magazine, self.page))

    def addstr(self, packet):
        r = packet.mrag.row
        if r:
            ParserNcurses(packet.displayable[:], self.scr, r)
        else:
            ParserNcurses(packet.header.displayable[:], self.scr, r)

    def do_alnum(self, i):
        if self.inputstate == 0:
            if i >= 1 and i <= 8:
                self.inputtmp[0] = i
                self.inputstate = 1
        else:
            self.inputtmp[self.inputstate] = i
            self.inputstate += 1

        if self.inputstate != 0:
            self.set_input_field(
                'P' + ''.join([('%1X' % self.inputtmp[x]) if self.inputtmp[x] is not None else '.' for x in range(3)]),
                3 if self.inputstate < 3 else 0)

        if self.inputstate == 3:
            self.inputstate = 0
            self.magazine = self.inputtmp[0]
            self.page = (self.inputtmp[1] << 4) | self.inputtmp[2]
            self.inputtmp = [None, None, None]
            self.last_header = None
            self.need_clear = True

    def do_hold(self):
        self.hold = not self.hold
        if self.hold:
            self.set_input_field('HOLD', 2)
            self.inputstate = 0
            self.inputtmp[0] = None
            self.inputtmp[1] = None
            self.inputtmp[2] = None
        else:
            self.set_input_field('P%d%02x' % (self.magazine, self.page))
            self.need_clear = True

    def do_reveal(self):
        self.reveal = not self.reveal
        self.set_concealed_pairs(self.reveal)

    def do_input(self, c):
        if c >= ord('0') and c <= ord('9'):
            if self.hold:
                self.do_hold()
            self.do_alnum(c - ord('0'))
        elif c >= ord('a') and c <= ord('f'):
            if self.hold:
                self.do_hold()
            self.do_alnum(c + 10 - ord('a'))
        elif c == ord('.'):
            self.do_hold()
        elif c == ord('r'):
            self.do_reveal()
        elif c == ord('h'):
            self.go_page(self.links[0].magazine, self.links[0].page)
        elif c == ord('j'):
            self.go_page(self.links[1].magazine, self.links[1].page)
        elif c == ord('k'):
            self.go_page(self.links[2].magazine, self.links[2].page)
        elif c == ord('l'):
            self.go_page(self.links[3].magazine, self.links[3].page)
        elif c == ord('q'):
            self.running = False

    def handle_one_packet(self):

        packet = next(self.packet_iter)
        if self.inputstate == 0 and not self.hold:
            if packet.mrag.magazine == self.magazine:
                if packet.mrag.row == 0:
                    if packet.header.page == self.page:
                        if self.need_clear or packet.header.control & 0x8:
                            self.scr.erase()
                            self.need_clear = False
                    self.last_header = packet.header.page
                    self.addstr(packet)
                    self.set_input_field('P%d%02X' % (self.magazine, self.page))
                elif self.last_header == self.page:
                    if packet.mrag.row < 25:
                        self.addstr(packet)
                    elif packet.mrag.row == 27:
                        self.links = packet.fastext.links
                        #print(self.links)

    def main(self):
        self.running = True
        while self.running:
            for i in range(32):
                self.handle_one_packet()

            self.do_input(self.scr.getch())

            self.scr.refresh()
            time.sleep(0.01)


def main(input, initial_page):
    locale.setlocale(locale.LC_ALL, '')

    input_dup = os.fdopen(os.dup(input.fileno()), 'rb')
    if os.name == 'nt':
        f = open("CON:", 'r')
    else:
        f = open("/dev/tty", 'r')
    os.dup2(f.fileno(), 0)

    chunks = FileChunker(input_dup, 42, loop=True)
    packets = (Packet(data, number) for number, data in chunks)

    def main(scr):
        Interactive(packets, scr, initial_page=initial_page).main()

    try:
        curses.wrapper(main)
    except TerminalTooSmall as e:
        print(f'Your terminal is too small.\nPlease make it at least 41x25.\nCurrent size: {e.args[0]}x{e.args[1]}.')
        exit(-1)
