#!/usr/bin/env python
"""Django command-line utility for the Agentic Platform."""
import os
import sys


def main():
    os.environ.setdefault("DJANGO_SETTINGS_MODULE", "agentic_platform.settings")
    from django.core.management import execute_from_command_line

    execute_from_command_line(sys.argv)


if __name__ == "__main__":
    main()
