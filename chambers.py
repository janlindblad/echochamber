# Echochamber
#   - Group chats for BlueSky
#
# (C) 2025 All For Eco AB, Jan Lindblad
# See LICENSE for license conditions

import os, glob, json, logging

log = logging.getLogger("echochamber.chambers")

class Chambers:
    @staticmethod
    def make_chamber_file_path(handle):
        datadir = os.environ.get("ECHOCHAMBER_DATADIR", ".")
        return f"{datadir}/{handle}.chamber"

    @staticmethod
    def get_chamber_files():
        file_pattern = Chambers.make_chamber_file_path("*")
        return glob.glob(file_pattern)

    @staticmethod
    def get_definitions():
        chambers = {}
        for chamber_filename in Chambers.get_chamber_files():
            try:
                with open(chamber_filename) as f:
                    log.info(f"Loading {chamber_filename}")
                    json_text = f.read()
                    handle = os.path.splitext(os.path.basename(chamber_filename))[0]
                    chambers[handle] = json.loads(json_text)
            except Exception as e:
                log.exception(f"Loading {handle} failed, skipping.", exc_info=e)
        return chambers

    @staticmethod
    def delete(handle):
        log.info(f"Deleting {handle}")
        os.unlink(Chambers.make_chamber_file_path(handle))

    @staticmethod
    def create(chamber_name, username, password, hostname, handle):
        #Check if exists
        pass
