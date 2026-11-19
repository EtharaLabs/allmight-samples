#!/usr/bin/env python3
"""Run a command as another user (a runuser replacement for slim images): asuser.py <user> <cmd> [args...]"""
import os
import pwd
import sys


def main() -> None:
    pw = pwd.getpwnam(sys.argv[1])
    os.initgroups(pw.pw_name, pw.pw_gid)
    os.setgid(pw.pw_gid)
    os.setuid(pw.pw_uid)
    os.environ["HOME"], os.environ["USER"], os.environ["LOGNAME"] = pw.pw_dir, pw.pw_name, pw.pw_name
    os.execvp(sys.argv[2], sys.argv[2:])


if __name__ == "__main__":
    main()
