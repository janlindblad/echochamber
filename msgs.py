# Echo chamber
#   - Group chats for BlueSky
#
# (C) 2025 All For Eco AB, Jan Lindblad
# See LICENSE for license conditions

class ShutdownMsg:
    def __init__(self, handle):
        self.handle = handle

class StartupMsg:
    def __init__(self, handle, username, password, hostname):
        self.handle   = handle
        self.username = username
        self.password = password
        self.hostname = hostname
