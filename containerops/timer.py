from io import StringIO
from pyinfra.api import operation
from pyinfra.operations import files, systemd

@operation()
def schedule_command(timer_name: str, on_calendar: str, command: str):
    command = command.replace('"', '\\"')
    timer_config = f"""[Unit]
Description=containerops timer {timer_name}

[Timer]
OnCalendar={on_calendar}
Persistent=true

[Install]
WantedBy=timers.target
"""
    
    unit_config = f"""[Unit]
Description=containerops timer {timer_name} command

[Service]
ExecStart=/bin/sh -c "{command}"
"""
    
    yield from files.put._inner(src=StringIO(timer_config), dest=f'/etc/systemd/system/containerops-timer-{timer_name}.timer')
    yield from files.put._inner(src=StringIO(unit_config), dest=f'/etc/systemd/system/containerops-timer-{timer_name}.service')
    yield from systemd.service._inner(f'containerops-timer-{timer_name}.timer', running=True, enabled=True)
