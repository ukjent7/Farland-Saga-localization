"""Linux process checks for game files installed on a Windows volume."""
import os
from pathlib import Path


GAME_NAMES = {'farlandsaga.exe', 's-tribute.exe'}


def running_game_processes():
    """Return visible Linux PIDs running the game under Wine/Proton, if any."""
    if os.name != 'posix' or not Path('/proc').is_dir():
        raise RuntimeError('Run project operations from the WSL/Linux environment.')
    found = []
    for entry in Path('/proc').iterdir():
        if not entry.name.isdecimal():
            continue
        try:
            comm = (entry / 'comm').read_text(encoding='utf-8', errors='replace').strip().lower()
            arguments = (entry / 'cmdline').read_bytes().split(b'\0')
        except OSError:
            continue
        names = {argument.decode('utf-8', errors='replace').replace('\\', '/').rsplit('/', 1)[-1].lower()
                 for argument in arguments if argument}
        if comm in GAME_NAMES or names.intersection(GAME_NAMES):
            found.append(entry.name)
    return sorted(found, key=int)


def require_no_linux_game_process():
    processes = running_game_processes()
    if processes:
        raise RuntimeError('The game is running in Linux/Wine. Close it before writing game files: ' + ', '.join(processes))
    return processes
