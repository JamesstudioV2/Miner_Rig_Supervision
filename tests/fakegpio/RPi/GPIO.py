"""Fake RPi.GPIO for tests: the pin levels live in a shared memory file (like /dev/gpiomem),
so several processes really share - and can fight over - the same pins.

FAKE_GPIOMEM: path of the 64 byte level file (created when missing)
FAKE_GPIO_ALT: "7:SPI,14:SERIAL" - pins in an alternative (kernel) function
FAKE_GPIO_LOG: file that receives one "pid pin level" line per output write
"""
import mmap
import os

BCM, BOARD = 11, 10
OUT, IN = 0, 1
HIGH, LOW = 1, 0
PUD_OFF, PUD_DOWN, PUD_UP = 20, 21, 22
SERIAL, SPI, I2C, HARD_PWM, UNKNOWN = 40, 41, 42, 43, -1
VERSION = "fake"

_path = os.environ.get("FAKE_GPIOMEM", "/tmp/fake-gpiomem")
if not os.path.exists(_path) or os.path.getsize(_path) < 64:
    with open(_path, "wb") as f:
        f.write(b"\0" * 64)
_f = open(_path, "r+b")
_mem = mmap.mmap(_f.fileno(), 64)
_modes = {}
_alt = {}
for item in filter(None, os.environ.get("FAKE_GPIO_ALT", "").split(",")):
    pin, func = item.split(":")
    _alt[int(pin)] = globals()[func]


def setwarnings(flag):
    pass


def setmode(mode):
    pass


def gpio_function(pin):
    if pin in _modes:
        return _modes[pin]
    return _alt.get(pin, IN)


def setup(pin, direction, pull_up_down=PUD_OFF, initial=None):
    _modes[pin] = direction
    _alt.pop(pin, None)
    if direction == OUT and initial is not None:
        output(pin, initial)
    elif direction == IN and pull_up_down in (PUD_UP, PUD_DOWN) and os.environ.get("FAKE_GPIO_PULL") == "1":
        _mem[pin] = 1 if pull_up_down == PUD_UP else 0


def output(pin, value):
    if _modes.get(pin) != OUT:
        raise RuntimeError("The GPIO channel has not been set up as an OUTPUT")
    _mem[pin] = 1 if value else 0
    log = os.environ.get("FAKE_GPIO_LOG")
    if log:
        with open(log, "a") as f:
            f.write("%d %d %d\n" % (os.getpid(), pin, 1 if value else 0))


def input(pin):  # noqa: A001 - library API
    if pin not in _modes:
        raise RuntimeError("You must setup() the GPIO channel first")
    return _mem[pin]


def cleanup(pin=None):
    if pin is None:
        _modes.clear()
    else:
        _modes.pop(pin, None)
