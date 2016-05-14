#!/usr/bin/env python

import atexit
import serial
import os
import logging
import logging.handlers
import sys
import time
import subprocess
import sh
import signal
import re
import config_server

from dcnow import DreamcastNowService

from datetime import datetime, timedelta


logger = logging.getLogger('dreampi')


def get_default_iface_name_linux():
    route = "/proc/net/route"
    with open(route) as f:
        for line in f.readlines():
            try:
                iface, dest, _, flags, _, _, _, _, _, _, _, =  line.strip().split()
                if dest != '00000000' or not int(flags, 16) & 2:
                    continue
                return iface
            except:
                continue


def ip_exists(ip, iface):
    command = [ "arping", "-c", "1", "-f", "-I", iface, ip ]

    try:
        output = subprocess.check_output(command)
    except subprocess.CalledProcessError as e:
        # Arping seems to always return 1 ?
        if e.returncode != 1:
            logging.exception("Error detecting ips")
            return True #Assume the worst
        else:
            output = e.output

    print output
    try:
        response_line = output.strip().split("\n")[-1]
        response_count = int(response_line.split(" ")[1])
        return response_count > 0
    except (IndexError, TypeError, ValueError):
        return True # Worst case


def find_next_unused_ip(start):
    interface = get_default_iface_name_linux()

    parts = [int(x) for x in start.split(".")]
    current_check = parts[-1] - 1

    while current_check:
        test_ip = ".".join([str(x) for x in parts[:3] + [current_check]])
        if not ip_exists(test_ip, interface):
            return test_ip
        current_check -= 1

    raise Exception("Unable to find a free IP on the network")


def autoconfigure_ppp(device, speed):
    """
       Every network is different, this function runs on boot and tries
       to autoconfigure PPP as best it can by detecting the subnet and gateway
       we're running on.

       Returns the IP allocated to the Dreamcast
    """

    gateway_ip = subprocess.check_output("route -n | grep 'UG[ \t]' | awk '{print $2}'", shell=True)
    subnet = gateway_ip.split(".")[:3]


    PEERS_TEMPLATE = """
{device}
{device_speed}
{this_ip}:{dc_ip}
noauth
    """.strip()

    OPTIONS_TEMPLATE = """
debug
ms-dns {}
proxyarp
ktune
noccp
    """.strip()

    this_ip = find_next_unused_ip(".".join(subnet) + ".100")
    dreamcast_ip = find_next_unused_ip(this_ip)

    logger.info("Dreamcast IP: {}".format(dreamcast_ip))

    peers_content = PEERS_TEMPLATE.format(device=device, device_speed=speed, this_ip=this_ip, dc_ip=dreamcast_ip)

    with open("/etc/ppp/peers/dreamcast", "w") as f:
        f.write(peers_content)

    options_content = OPTIONS_TEMPLATE.format(this_ip)

    with open("/etc/ppp/options", "w") as f:
        f.write(options_content)

    return dreamcast_ip


def detect_device_and_speed():
    command = [ "wvdialconf", "/dev/null" ]

    try:
        output = subprocess.check_output(command, stderr=subprocess.STDOUT)

        lines = output.split("\n")

        for line in lines:
            match = re.match("(.+)\<Info\>\:\sSpeed\s(\d+);", line.strip())
            if match:
                device = match.group(1)
                speed = match.group(2)
                logger.info("Detected device {} with speed {}".format(device, speed))
                return device, speed
        else:
            logger.info("No device detected")

    except:
        logger.exception("Unable to detect modem. Falling back to ttyACM0")
    return ("ttyACM0", 460800)


class Daemon(object):
    def __init__(self, pidfile, process):
        self.pidfile = pidfile
        self.process = process

    def daemonize(self):
        try:
            pid = os.fork()
            if pid > 0:
                sys.exit(0)

        except OSError, e:
            sys.exit(1)

        os.chdir("/")
        os.setsid()
        os.umask(0)

        try:
            pid = os.fork()
            if pid > 0:
                sys.exit(0)
        except OSError, e:
            sys.exit(1)

        atexit.register(self.delete_pid)
        pid = str(os.getpid())
        with open(self.pidfile, 'w+') as f:
            f.write("%s\n" % pid)

    def delete_pid(self):
        os.remove(self.pidfile)

    def _read_pid_from_pidfile(self):
        try:
            with open(self.pidfile, 'r') as pf:
                pid = int(pf.read().strip())
        except IOError:
            pid = None
        return pid

    def start(self):
        pid = self._read_pid_from_pidfile()

        if pid:
            logger.info("Daemon already running, exiting")
            sys.exit(1)

        logger.info("Starting daemon")
        self.daemonize()
        self.run()

    def stop(self):
        pid = self._read_pid_from_pidfile()

        if not pid:
            logger.info("pidfile doesn't exist, deamon must not be running")
            return

        try:
            while True:
                os.kill(pid, signal.SIGTERM)
                time.sleep(0.1)

        except OSError as err:
            if os.path.exists(self.pidfile):
                os.remove(self.pidfile)
            else:
                sys.exit(1)

    def restart(self):
        self.stop()
        self.start()

    def run(self):
        self.process()


class Modem(object):
    def __init__(self, comm_speed=None, send_dial_tone=True):
        self._device, self._speed = detect_device_and_speed()
        self._comm_speed = comm_speed or self._speed
        self._serial = None
        self._sending_tone = False

        if send_dial_tone:
            self._dial_tone_wav = self._read_dial_tone()
        else:
            self._dial_tone_wav = None

        self._time_since_last_dial_tone = None
        self._dial_tone_counter = 0

    @property
    def device_speed(self):
        return self._speed

    @property
    def device_name(self):
        return self._device

    def _read_dial_tone(self):
        this_dir = os.path.dirname(os.path.abspath(os.path.realpath(__file__)))
        dial_tone_wav = os.path.join(this_dir, "dial-tone.wav")

        with open(dial_tone_wav, "rb") as f:
            dial_tone = f.read() # Read the entire wav file
            dial_tone = dial_tone[44:] # Strip the header (44 bytes)

        return dial_tone

    def connect(self):
        if self._serial:
            self.disconnect()

        logger.info("Opening serial interface to {}".format(self._device))
        self._serial = serial.Serial("/dev/{}".format(self._device), self._comm_speed, timeout=0)

    def disconnect(self):
        if self._serial and self._serial.isOpen():
            self._serial.close()
            self._serial = None
            logger.info("Serial interface terminated")

    def reset(self):
        self.send_command("ATZ0") # Send reset command
        self.send_command("ATE0") # Don't echo our responses

    def start_dial_tone(self):
        if not self._dial_tone_wav:
            return

        self.reset()
        self.send_command("AT+FCLASS=8") # Enter voice mode
        self.send_command("AT+VLS=1") # Go off-hook
        self.send_command("AT+VSM=1,8000") # 8 bit unsigned PCM
        self.send_command("AT+VTX") # Voice transmission mode

        self._sending_tone = True
        self._time_since_last_dial_tone = datetime.now() - timedelta(seconds=100)
        self._dial_tone_counter = 0

    def stop_dial_tone(self):
        if not self._sending_tone:
            return

        self._serial.write("\0{}{}\r\n".format(chr(0x10), chr(0x03)))
        self.send_escape()
        self.send_command("ATH0") # Go on-hook
        self.reset() #Reset the modem
        self._sending_tone = False

    def answer(self):
        self.reset()
        self.send_command("ATA")
        time.sleep(2)
        logger.info("Call answered!")
        logger.info(subprocess.check_output(["pon", "dreamcast"]))
        logger.info("Connected")

    def send_command(self, command, timeout=30):
        VALID_RESPONSES = ("OK", "ERROR", "CONNECT", "VCON")

        final_command = "%s\r\n" % command
        self._serial.write(final_command)
        logger.info(final_command)

        start = datetime.now()

        line = ""
        while True:
            new_data = self._serial.readline().strip()

            if not new_data:
                continue

            line = line + new_data
            for resp in VALID_RESPONSES:
                if resp in line:
                    logger.info(line[line.find(resp):])
                    return # We are done

            if (datetime.now() - start).total_seconds() > timeout:
                raise IOError("There was a timeout while waiting for a response from the modem")

    def send_escape(self):
        time.sleep(1.0)
        self._serial.write("+++")
        time.sleep(1.0)

    def update(self):
        now = datetime.now()
        if self._sending_tone:
            # Keep sending dial tone
            BUFFER_LENGTH = 1000
            TIME_BETWEEN_UPLOADS_MS = (1000.0 / 8000.0) * BUFFER_LENGTH

            milliseconds = (now - self._time_since_last_dial_tone).microseconds * 1000
            if not self._time_since_last_dial_tone or milliseconds >= TIME_BETWEEN_UPLOADS_MS:
                byte = self._dial_tone_wav[self._dial_tone_counter:self._dial_tone_counter+BUFFER_LENGTH]
                self._dial_tone_counter += BUFFER_LENGTH
                if self._dial_tone_counter >= len(self._dial_tone_wav):
                    self._dial_tone_counter = 0
                self._serial.write(byte)
                self._time_since_last_dial_tone = now



def process():
    dial_tone_enabled = not "--disable-dial-tone" in sys.argv

    # Make sure pppd isn't running
    with open(os.devnull, 'wb') as devnull:
        subprocess.call(["sudo", "killall", "pppd"], stderr=devnull)

    BAUD_SPEED = 57600
    modem = Modem(BAUD_SPEED, dial_tone_enabled)
    dreamcast_ip = autoconfigure_ppp(modem.device_name, modem.device_speed)

    mode = "LISTENING"

    modem.connect()
    if dial_tone_enabled:
        modem.start_dial_tone()

    time_digit_heard = None

    dcnow = DreamcastNowService()

    while True:
        now = datetime.now()

        if mode == "LISTENING":
            modem.update()
            char = modem._serial.read(1).strip()
            if not char:
                continue

            if ord(char) == 16:
                #DLE character
                try:
                    char = modem._serial.read(1)
                    digit = int(char)
                    logger.info("Heard: %s", digit)

                    mode = "ANSWERING"
                    modem.stop_dial_tone()
                    time_digit_heard = now
                except (TypeError, ValueError):
                    pass
        elif mode == "ANSWERING":
            if (now - time_digit_heard).total_seconds() > 8.0:
                time_digit_heard = None
                modem.answer()
                modem.disconnect()
                mode = "CONNECTED"

        elif mode == "CONNECTED":
            dcnow.go_online(dreamcast_ip)

            # We start watching /var/log/messages for the hang up message
            for line in sh.tail("-f", "/var/log/messages", "-n", "1", _iter=True):
                if "Modem hangup" in line:
                    logger.info("Detected modem hang up, going back to listening")
                    time.sleep(5) # Give the hangup some time
                    break

            dcnow.go_offline()

            mode = "LISTENING"
            modem = Modem(BAUD_SPEED, dial_tone_enabled)
            modem.connect()
            if dial_tone_enabled:
                modem.start_dial_tone()

    return 0


def main():
    try:
        config_server.start()
        return process()
    except:
        config_server.stop()
        logger.exception("Something went wrong...")
        return 1


if __name__ == '__main__':
    logger.setLevel(logging.INFO)
    handler = logging.handlers.SysLogHandler(address='/dev/log')
    logger.addHandler(handler)

    if len(sys.argv) > 1 and "--no-daemon" in sys.argv:
        logger.addHandler(logging.StreamHandler())
        sys.exit(main())

    daemon = Daemon("/tmp/dreampi.pid", main)

    if len(sys.argv) == 2:
        if sys.argv[1] == "start":
            daemon.start()
        elif sys.argv[1] == "stop":
            daemon.stop()
        elif sys.argv[1] == "restart":
            daemon.restart()
        else:
            sys.exit(2)
        sys.exit(0)
    else:
        print("Usage: %s start|stop|restart" % sys.argv[0])
        sys.exit(2)
