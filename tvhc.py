#!/usr/bin/env python3

"""tvhc — watch live TV from a TVHeadend server.

A small Tkinter client that shows an EPG grid and plays channels with mpv.
"""

import collections
import configparser
import html
import json
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from types import SimpleNamespace

import requests
import tkinter as tk
from tkinter import ttk, font


class TVHCError(Exception):
    """An error that can be shown in the status bar instead of crashing."""


# --------------------------------------------------------------- config ---

CONFIG_PATH = os.path.expanduser('~/.tvhc.ini')

config = configparser.ConfigParser(allow_unnamed_section=True)
config.read(CONFIG_PATH)
try:
    config = config[configparser.UNNAMED_SECTION]
except KeyError:
    config = {}

if len(sys.argv) > 1:
    reqAuth = None
    TVHE_Server = sys.argv[1]
else:
    try:
        server = config.get('Server')
    except configparser.Error:
        server = None
    if not server:
        print(f'No Server configured in {CONFIG_PATH} and no server given on the command line.')
        sys.exit(1)

    proto = "https" if config.getboolean('UseTLS', fallback=False) else "http"
    TVHE_Server = f"{proto}://{server}/"
    username = config.get('Username', '')
    password = config.get('Password', '')
    a = config.get('Auth', 'Digest' if username or password else 'None')
    if a == 'None':
        reqAuth = None
    elif a == 'Digest':
        reqAuth = requests.auth.HTTPDigestAuth(username, password)
    elif a == 'Basic' or a == 'Plain':
        reqAuth = requests.auth.HTTPBasicAuth(username, password)
    else:
        print(f'Unrecognized Auth value "{a}" in {CONFIG_PATH}.')
        sys.exit(1)


def requests_get(*args, **kwargs):
    kwargs.setdefault('auth', reqAuth)
    kwargs.setdefault('timeout', 15)
    try:
        resp = requests.get(*args, **kwargs)
        resp.raise_for_status()
        return resp
    except requests.exceptions.HTTPError as err:
        code = err.response.status_code if err.response is not None else '?'
        if code == 401:
            raise TVHCError('401 Unauthorized — check Username/Password/Auth in the config') from err
        raise TVHCError(f'HTTP error {code} from the server') from err
    except requests.exceptions.ConnectionError as err:
        raise TVHCError(f'cannot reach {TVHE_Server} — will retry') from err
    except requests.exceptions.Timeout as err:
        raise TVHCError(f'timed out contacting {TVHE_Server} — will retry') from err
    except requests.exceptions.RequestException as err:
        raise TVHCError(str(err)[:160]) from err


# number of half-hour time slots to show in the scrollable grid
NSLOTS = 6
SLOT_SECONDS = 30 * 60

# column weight floor; very short programs must not stretch their row
MIN_COL_WEIGHT = 300


MPV_SOCKET = os.path.join(tempfile.gettempdir(), f'tvhc-mpv-{os.getpid()}.sock')


def mpv_command(payload):
    """Send one JSON command to the running mpv and consume its reply."""
    if not os.path.exists(MPV_SOCKET):
        raise OSError('mpv IPC socket is not available')
    sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
    sock.settimeout(5)
    try:
        sock.connect(MPV_SOCKET)
        payload = dict(payload, request_id=0)
        sock.sendall((json.dumps(payload) + '\n').encode('utf-8'))
        # each command produces exactly one newline-terminated reply
        reply = b''
        while not reply.endswith(b'\n'):
            chunk = sock.recv(4096)
            if not chunk:
                break
            reply += chunk
    finally:
        sock.close()


def kill_player():
    """Stop the mpv process and clean up its IPC socket, if any."""
    global player_process
    process = player_process
    player_process = None
    if process is not None and process.poll() is None:
        process.terminate()
        try:
            process.wait(timeout=2)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait()
    if os.path.exists(MPV_SOCKET):
        try:
            os.unlink(MPV_SOCKET)
        except OSError:
            pass


def ensure_player():
    """Start the single persistent mpv, if it is not already running."""
    global player_process
    if player_process is not None and player_process.poll() is None:
        return player_process

    if os.path.exists(MPV_SOCKET):
        try:
            os.unlink(MPV_SOCKET)
        except OSError:
            pass

    player_process = subprocess.Popen(
        ['mpv', '--quiet', '--idle=yes', f'--input-ipc-server={MPV_SOCKET}'])

    # mpv creates its IPC socket shortly after startup
    deadline = time.time() + 2
    while not os.path.exists(MPV_SOCKET):
        if player_process.poll() is not None:
            kill_player()
            raise OSError('mpv exited before creating its IPC socket')
        if time.time() > deadline:
            break
        time.sleep(0.02)

    if not os.path.exists(MPV_SOCKET):
        kill_player()
        raise OSError('mpv did not create its IPC socket in time')
    return player_process


def start_player(uuid, title):
    response = requests_get(f'{TVHE_Server}/play/ticket/stream/channel/{uuid}',
                            headers={'User-Agent': 'ticket-please'})
    urls = [line for line in response.text.splitlines()
            if (line and not line.startswith('#'))]
    if not urls:
        raise TVHCError('the server did not return a stream URL')

    try:
        mpv_command({'command': ['set_property', 'title', title]})
    except OSError:
        # the player is gone — spawn a fresh one and retry
        kill_player()
        ensure_player()
        mpv_command({'command': ['set_property', 'title', title]})

    print(f'playing {urls} in mpv')
    mpv_command({'command': ['loadfile', urls[0], 'replace']})
    for url in urls[1:]:
        mpv_command({'command': ['loadfile', url, 'append-play']})
    return player_process


def channel_sort_key(item):
    # sort by channel number as a float so that 9.9 comes before 10.1,
    # keeping unnumbered/non-numeric channels at the end.
    number, name = item[0]
    try:
        return (0, float(number), name)
    except (TypeError, ValueError):
        return (1, 0.0, name)


# fetch epg data from the TVH server
def get_epg_data(fstart=None, fend=None):
    response = requests_get(f'{TVHE_Server}/api/epg/events/grid?limit=99999')
    try:
        epg = response.json()
    except ValueError as err:
        raise TVHCError(f'invalid EPG response: {err}') from err

    # lists of program events using channel num,name as key
    channels = collections.defaultdict(list)

    # Filter out events that are outside grid start/end times.
    # Events come from the server sorted by start time, so when we
    # find one with start after the end of the grid, we're done.
    for e in epg.get('entries', []):
        try:
            key = e['channelNumber'], e['channelName']
        except KeyError:
            continue
        if 'start' not in e or 'stop' not in e:
            continue
        if fstart and e['stop'] < fstart:
            continue
        if fend and e['start'] > fend:
            break
        channels[key].append(e)

    return dict(sorted(channels.items(), key=channel_sort_key))


# fetch the full channel list, including channels with no EPG data
def get_channels():
    response = requests_get(f'{TVHE_Server}/api/channel/grid?start=0&limit=99999')
    try:
        data = response.json()
    except ValueError as err:
        raise TVHCError(f'invalid channel response: {err}') from err

    channels = {}
    for channel in data.get('entries', []):
        try:
            key = str(channel['number']), channel['name']
        except KeyError:
            continue
        if not channel.get('enabled', True):
            continue
        channels[key] = channel.get('uuid')
    return channels


# ---------------------------------------------------------------- theme ---

BG = "#1b1d21"
PANEL = "#23262b"
CELL = "#25282e"
CELL_HOVER = "#31353c"
HEADER_BG = "#2b2f36"
BORDER = "#3a3f47"
FG = "#e6e6e6"
MUTED = "#9aa0a6"
ACCENT = "#4c9aff"
ON_ACCENT = "#ffffff"
SELECT_BG = "#3a4250"
NOW_COLOR = "#e5534b"
ERROR = "#ff7b72"

root = tk.Tk()
root.title("TVHC — TVHeadend Client")
root.geometry("1020x660")
root.minsize(900, 420)
root.configure(background=BG)

style = ttk.Style(root)
style.theme_use("clam")

base_font = font.nametofont("TkDefaultFont").copy()
base_font.configure(size=10)
bold_font = base_font.copy()
bold_font.configure(weight="bold")
title_font = base_font.copy()
title_font.configure(size=14, weight="bold")

style.configure(".", background=BG, foreground=FG, fieldbackground=CELL,
                bordercolor=BORDER, focuscolor=ACCENT, troughcolor=PANEL,
                selectbackground=SELECT_BG, selectforeground=FG)
style.configure("TFrame", background=BG)
style.configure("TLabel", background=BG, foreground=FG, font=base_font)
style.configure("TButton", background=PANEL, foreground=FG,
                borderwidth=1, padding=(10, 3))
style.map("TButton",
          background=[("active", CELL_HOVER), ("pressed", SELECT_BG)],
          foreground=[("disabled", MUTED)])
style.configure("TEntry", fieldbackground=CELL, foreground=FG,
                insertcolor=FG, bordercolor=BORDER, padding=4)
style.map("TEntry", bordercolor=[("focus", ACCENT)])
style.configure("TCheckbutton", background=BG, foreground=FG,
                indicatorbackground=CELL, indicatorforeground=ACCENT,
                focuscolor=ACCENT)
style.map("TCheckbutton",
          background=[("active", BG)],
          indicatorbackground=[("selected", ACCENT), ("active", CELL_HOVER)],
          indicatorforeground=[("selected", ON_ACCENT)])
style.configure("Vertical.TScrollbar", background=PANEL, troughcolor=BG,
                bordercolor=BORDER, arrowcolor=MUTED,
                darkcolor=BORDER, lightcolor=BORDER)
style.map("Vertical.TScrollbar", background=[("active", CELL_HOVER)])

style.configure("Bold.TLabel", font=bold_font)
style.configure("Title.TLabel", font=title_font)
style.configure("Muted.TLabel", foreground=MUTED)
style.configure("HeadChan.TLabel", background=HEADER_BG, foreground=MUTED,
                font=bold_font, padding=(8, 4), bordercolor=BORDER)
style.configure("Slot.TLabel", background=HEADER_BG, foreground=MUTED,
                padding=(2, 4), bordercolor=BORDER)
style.configure("Chan.TLabel", background=CELL, foreground=FG,
                font=bold_font, padding=(8, 6), bordercolor=BORDER)
style.configure("ChanSel.TLabel", background=SELECT_BG, foreground=FG,
                font=bold_font, padding=(8, 6), bordercolor=ACCENT)
style.configure("ChanPlay.TLabel", background=ACCENT, foreground=ON_ACCENT,
                font=bold_font, padding=(8, 6), bordercolor=ACCENT)
style.configure("Prog.TLabel", background=CELL, foreground=FG,
                padding=(6, 4), bordercolor=BORDER)
style.configure("ProgHover.TLabel", background=CELL_HOVER, foreground=FG,
                padding=(6, 4), bordercolor=ACCENT)
style.configure("Empty.TLabel", background=CELL, foreground=MUTED,
                padding=(8, 4), bordercolor=BORDER)
style.configure("Status.TLabel", background=PANEL, foreground=MUTED,
                padding=(10, 4))
style.configure("Panel.TFrame", background=PANEL)
style.configure("Err.TLabel", background=PANEL, foreground=ERROR,
                padding=(10, 4))
style.configure("Play.TLabel", background=PANEL, foreground=FG,
                padding=(10, 4))


# ------------------------------------------------------------------ ui ---

status_bar = ttk.Frame(root, style="Panel.TFrame")
status_bar.pack(side="bottom", fill="x")
status_left = ttk.Label(status_bar, text="Ready", style="Status.TLabel")
status_left.pack(side="left")
status_right = ttk.Label(status_bar, text="Connecting…", style="Status.TLabel")
status_right.pack(side="right")

toolbar = ttk.Frame(root, padding=(10, 8, 10, 4))
toolbar.pack(side="top", fill="x")
toolbar.columnconfigure(0, weight=0)
toolbar.columnconfigure(1, weight=1)
toolbar.columnconfigure(2, weight=0)

search_box = ttk.Frame(toolbar)
search_box.grid(row=0, column=0, sticky="w")
ttk.Label(search_box, text="Search", style="Muted.TLabel").pack(side="left", padx=(0, 6))
search_var = tk.StringVar()
search_entry = ttk.Entry(search_box, textvariable=search_var, width=26)
search_entry.pack(side="left")
ttk.Button(search_box, text="Clear", command=lambda: search_var.set()).pack(side="left", padx=(6, 0))

epg_only_var = tk.BooleanVar(value=False)
epg_only_check = ttk.Checkbutton(toolbar, text="Only channels with EPG",
                                 variable=epg_only_var)
epg_only_check.grid(row=0, column=1)

right_box = ttk.Frame(toolbar)
right_box.grid(row=0, column=2, sticky="e")

clock_label = ttk.Label(right_box, text="", style="Bold.TLabel")
clock_label.pack(side="right", padx=(12, 0))
date_label = ttk.Label(right_box, text="", style="Muted.TLabel")
date_label.pack(side="right", padx=(12, 0))

nav_box = ttk.Frame(right_box)
nav_box.pack(side="right", padx=(0, 12))
prev_button = ttk.Button(nav_box, text="◀", width=3)
prev_button.pack(side="left")
now_button = ttk.Button(nav_box, text="Now")
now_button.pack(side="left", padx=4)
next_button = ttk.Button(nav_box, text="▶", width=3)
next_button.pack(side="left")

container = ttk.Frame(root, padding=(10, 0, 10, 0))
container.pack(fill="both", expand=True)
container.columnconfigure(0, weight=1)
container.rowconfigure(1, weight=1)

# header, aligned with the channel column and the program area
header = ttk.Frame(container)
header.grid(row=0, column=0, sticky="nsew")
header.columnconfigure(1, weight=1)
header_chan = ttk.Label(header, text="Channel", style="HeadChan.TLabel", anchor="w")
header_chan.grid(row=0, column=0, sticky="nsew")
header_progs = ttk.Frame(header)
header_progs.grid(row=0, column=1, sticky="nsew")

slot_labels = []
for i in range(NSLOTS):
    label = ttk.Label(header_progs, anchor="center", borderwidth=1,
                      relief="solid", style="Slot.TLabel")
    label.grid(row=0, column=i, sticky="nsew")
    header_progs.columnconfigure(i, weight=SLOT_SECONDS, uniform="slots")
    slot_labels.append(label)

body = ttk.Frame(container)
body.grid(row=1, column=0, sticky="nsew")
body.columnconfigure(0, weight=1)
body.rowconfigure(0, weight=1)

canvas = tk.Canvas(body, background=BG, highlightthickness=0, bd=0)
canvas.grid(row=0, column=0, sticky="nsew")
scrollbar = ttk.Scrollbar(body, orient="vertical", command=canvas.yview)
scrollbar.grid(row=0, column=1, sticky="ns")
canvas.configure(yscrollcommand=scrollbar.set)

scroll_frame = ttk.Frame(canvas)
scroll_frame.grid_columnconfigure(1, weight=1)
canvas_window = canvas.create_window((0, 0), window=scroll_frame, anchor="nw")
# a canvas line would be hidden behind the embedded scroll_frame widget,
# so use a thin sibling frame that is lifted above it
now_indicator = tk.Frame(canvas, background=NOW_COLOR, width=2)


# --------------------------------------------------------------- state ---

now = None
view_start = None
view_end = None
follow_now = True

gridrows = {}          # channel key -> row widgets/metadata
visible_keys = []      # channel keys currently shown (after filtering)
selected_key = None    # keyboard selection
playing_key = None     # channel key currently playing
player_process = None  # the single persistent mpv process
popup = None           # program details Toplevel
server_ok = False      # last EPG request succeeded
all_channels = None    # full channel list, fetched once


# ------------------------------------------------------------- helpers ---

def thm(t):
    return time.strftime("%-I:%M %p", time.localtime(t))


def thd(t):
    return time.strftime("%a %b %-d", time.localtime(t))


def clean_text(text, default=''):
    if not text:
        return default
    return html.unescape(str(text))


def show_status(text, style_name="Status.TLabel"):
    status_left.configure(text=text, style=style_name)


def show_connection(text, error=False):
    status_right.configure(text=text, style="Err.TLabel" if error else "Status.TLabel")


# ----------------------------------------------------------------- grid ---

def rebuild_rows(keys):
    for child in scroll_frame.winfo_children():
        child.destroy()
    gridrows.clear()

    for key in keys:
        number, name = key
        chan = ttk.Label(scroll_frame, text=f"{number}   {clean_text(name)}",
                         anchor="w", borderwidth=1, relief="solid",
                         style="Chan.TLabel", cursor="hand2")
        chan.metadata = SimpleNamespace(key=key, number=number, name=name)
        chan.bind("<Button-1>", play_stop)

        progs = ttk.Frame(scroll_frame)
        gridrows[key] = SimpleNamespace(chan=chan, progs=progs, number=number,
                                        name=name, uuid=None, has_epg=False)


def apply_filter(reset_scroll=False):
    global selected_key
    query = search_var.get().strip().lower()
    epg_only = epg_only_var.get()
    visible_keys.clear()
    index = 0

    for key, row in gridrows.items():
        matches = (not query or query in f"{row.number} {row.name}".lower())
        if epg_only and not row.has_epg:
            matches = False
        if matches:
            row.chan.grid(row=index, column=0, sticky="nsew")
            row.progs.grid(row=index, column=1, sticky="nsew")
            visible_keys.append(key)
            index += 1
        else:
            row.chan.grid_remove()
            row.progs.grid_remove()

    if selected_key not in visible_keys:
        selected_key = None

    refresh_row_styles()
    if reset_scroll:
        canvas.yview_moveto(0)
    root.after_idle(framesync)


def refresh_row_styles():
    for key, row in gridrows.items():
        if key == playing_key:
            row.chan.configure(style="ChanPlay.TLabel")
        elif key == selected_key:
            row.chan.configure(style="ChanSel.TLabel")
        else:
            row.chan.configure(style="Chan.TLabel")


def framesync(event=None):
    canvas.configure(scrollregion=canvas.bbox("all"))
    cwidth = scroll_frame.grid_bbox(0, 0)[2] if gridrows else 0
    header.columnconfigure(0, minsize=cwidth)
    update_nowline()


def update_header():
    if view_start is None:
        return
    for i, label in enumerate(slot_labels):
        label.configure(text=thm(view_start + i * SLOT_SECONDS))


def update_nowline():
    if now is None or view_start is None or not gridrows or not (view_start <= now <= view_end):
        now_indicator.place_forget()
        return

    channel_width = scroll_frame.grid_bbox(0, 0)[2] if gridrows else 0
    width = canvas.winfo_width()
    if width <= channel_width:
        now_indicator.place_forget()
        return

    x = channel_width + (now - view_start) / (view_end - view_start) * (width - channel_width)
    now_indicator.place(x=int(x) - 1, y=0, relheight=1)
    now_indicator.lift()


def update_epg_grid():
    global server_ok, all_channels
    try:
        epg = get_epg_data(view_start, view_end)
    except TVHCError as err:
        server_ok = False
        show_connection(f"⚠ {str(err)[:160]}", error=True)
        return
    server_ok = True
    show_connection("Connected")

    # the channel list is optional: if the request fails, fall back to
    # showing only the channels that appear in the EPG
    if all_channels is None:
        try:
            all_channels = get_channels()
        except TVHCError:
            all_channels = None

    channel_keys = set(epg)
    if all_channels:
        channel_keys.update(all_channels)
    keys = sorted(channel_keys, key=lambda key: channel_sort_key((key, None)))

    if set(keys) != set(gridrows):
        rebuild_rows(keys)

    for key in keys:
        row = gridrows.get(key)
        if row is None:
            continue
        events = epg.get(key, [])
        row.has_epg = bool(events)
        row.uuid = all_channels.get(key) if all_channels else None
        if events and events[0].get('channelUuid'):
            row.uuid = events[0]['channelUuid'] or row.uuid

        for child in row.progs.winfo_children():
            child.destroy()

        col = 0
        group = f'progs-{key}'
        pos = view_start

        if not events:
            empty = ttk.Label(row.progs, text="No program data",
                              borderwidth=1, relief="solid",
                              style="Empty.TLabel", cursor="hand2")
            empty.metadata = SimpleNamespace(key=key)
            empty.bind("<Button-1>", play_stop)
            empty.grid(row=0, column=0, sticky="nsew")
            row.progs.columnconfigure(0, weight=1, uniform=group)
            continue

        for event in events:
            title = clean_text(event.get('title'), '(no title)')
            start = max(event['start'], pos)
            stop = min(event['stop'], view_end)

            if start > pos:
                gap = start - pos
                filler = ttk.Frame(row.progs)
                filler.grid(row=0, column=col, sticky="nsew")
                row.progs.columnconfigure(col, weight=gap, uniform=group)
                pos += gap
                col += 1

            duration = stop - start
            # some stations schedule very small programs (e.g. 1 second)
            # and very small weights trigger an Xlib fault
            if duration < 10:
                continue

            display_title = title if len(title) <= 55 else title[:52].rstrip() + '…'
            description = clean_text(event.get('description'))
            subtitle = clean_text(event.get('subtitle'))
            if description:
                body = description
                subtitle_line = subtitle
            else:
                # some EPG sources put the long description in subtitle
                body = subtitle
                subtitle_line = ''
            label = ttk.Label(row.progs, text=display_title, borderwidth=1,
                              relief="solid", style="Prog.TLabel", cursor="hand2")
            label.metadata = SimpleNamespace(
                key=key,
                title=title,
                subtitle=subtitle_line,
                description=body or 'No description available.',
                start=event['start'],
                stop=event['stop'],
            )
            label.bind("<Button-1>", play_stop)
            label.bind("<Button-3>", program_popup)
            label.bind("<Enter>", program_enter)
            label.bind("<Leave>", program_leave)
            label.grid(row=0, column=col, sticky="nsew")
            # grid uniform groups size columns proportionally to
            # weight/requested width, so a very short program with a
            # long title can inflate the whole row (and the window)
            # to tens of thousands of pixels, which X rejects with
            # BadAlloc.  Give short programs a floor weight.
            row.progs.columnconfigure(col, weight=max(duration, MIN_COL_WEIGHT),
                                      uniform=group)
            pos = stop
            col += 1

        if pos < view_end:
            filler = ttk.Frame(row.progs)
            filler.grid(row=0, column=col, sticky="nsew")
            row.progs.columnconfigure(col, weight=view_end - pos, uniform=group)

    apply_filter()


# ----------------------------------------------------------- navigation ---

def time_window_start(t):
    return ((t - 31 * 60) // SLOT_SECONDS) * SLOT_SECONDS


def set_view(start, follow):
    global view_start, view_end, follow_now
    view_start = start
    view_end = start + NSLOTS * SLOT_SECONDS
    follow_now = follow
    update_epg_grid()
    update_header()
    update_nowline()


def page_view(delta):
    if view_start is not None:
        set_view(view_start + delta, follow=False)


def jump_to_now():
    global now
    now = int(time.time())
    set_view(time_window_start(now), follow=True)


def move_selection(delta):
    global selected_key
    if not visible_keys:
        return
    try:
        index = visible_keys.index(selected_key)
    except ValueError:
        index = -1 if delta > 0 else 0
    index = max(0, min(len(visible_keys) - 1, index + delta))
    selected_key = visible_keys[index]
    refresh_row_styles()
    scroll_to_key(selected_key)


def scroll_to_key(key):
    row = gridrows.get(key)
    if row is None:
        return
    root.update_idletasks()
    y = row.chan.winfo_y()
    height = row.chan.winfo_height()
    total = scroll_frame.winfo_height() or 1
    top = canvas.canvasy(0)
    view_height = canvas.winfo_height()
    if y < top:
        canvas.yview_moveto(y / total)
    elif y + height > top + view_height:
        canvas.yview_moveto((y + height - view_height) / total)


# ----------------------------------------------------------- playback ---

def set_playing(key):
    global playing_key
    playing_key = key
    refresh_row_styles()


def stop_playback():
    global playing_key
    if player_process is not None and player_process.poll() is None:
        try:
            mpv_command({'command': ['stop']})
        except OSError:
            kill_player()

    if playing_key is not None:
        playing_key = None
        refresh_row_styles()
        show_status("Ready")


def toggle_play(key, title=None, toggle=True):
    global player_process
    if key is None:
        return
    if key == playing_key:
        if toggle:
            stop_playback()
        return

    stop_playback()
    row = gridrows.get(key)
    if row is None:
        return
    if not row.uuid:
        show_connection("⚠ no channel id available", error=True)
        return

    try:
        process = start_player(row.uuid, f"{row.number} {row.name}")
    except (TVHCError, OSError) as err:
        show_connection(f"⚠ {str(err)[:160]}", error=True)
        return

    player_process = process
    set_playing(key)
    text = f"▶  {row.number} {clean_text(row.name)}"
    if title:
        text += f" — {title}"
    show_status(text, "Play.TLabel")


def play_stop(event):
    metadata = getattr(event.widget, 'metadata', None)
    if metadata is None:
        return
    global selected_key
    selected_key = metadata.key
    refresh_row_styles()
    # take keyboard focus back from the search box so the arrow keys and
    # Enter act on the guide after a click
    root.focus_set()
    title = getattr(metadata, 'title', None)
    # clicking a program of the channel that is already playing should not
    # stop playback; only the channel label acts as a play/stop toggle.
    toggle_play(metadata.key, title, toggle=title is None)


def program_enter(event):
    event.widget.configure(style="ProgHover.TLabel")


def program_leave(event):
    event.widget.configure(style="Prog.TLabel")


def player_exited():
    global player_process, playing_key
    process = player_process
    if process is None or process.poll() is None:
        return
    player_process = None
    if playing_key is not None:
        playing_key = None
        refresh_row_styles()
        show_status("Player closed")


def handle_sigchild(signum, frame):
    if player_process and player_process.poll() is not None:
        root.after(10, player_exited)


signal.signal(signal.SIGCHLD, handle_sigchild)


# -------------------------------------------------------------- popups ---

def close_popup():
    global popup
    if popup is not None and popup.winfo_exists():
        try:
            popup.grab_release()
        except tk.TclError:
            pass
        popup.destroy()
    popup = None


def program_popup(event):
    global popup
    metadata = getattr(event.widget, 'metadata', None)
    if metadata is None:
        return

    close_popup()
    row = gridrows.get(metadata.key)

    popup = tk.Toplevel(root)
    popup.title("Program details")
    popup.configure(background=BG)
    popup.transient(root)
    popup.resizable(False, False)

    frame = ttk.Frame(popup, padding=16)
    frame.pack(fill="both", expand=True)

    channel = f"{row.number}  {clean_text(row.name)}" if row else ""
    ttk.Label(frame, text=channel, style="Muted.TLabel").pack(anchor="w")
    ttk.Label(frame, text=metadata.title, style="Title.TLabel",
              wraplength=440, justify="left").pack(anchor="w", pady=(4, 8))

    subtitle = getattr(metadata, 'subtitle', '')
    if subtitle:
        ttk.Label(frame, text=subtitle, style="Muted.TLabel",
                  wraplength=440, justify="left").pack(anchor="w", pady=(0, 8))

    length = round((metadata.stop - metadata.start) / 60)
    ttk.Label(frame, text=f"{thm(metadata.start)} – {thm(metadata.stop)}  ·  {length} min",
              style="Muted.TLabel").pack(anchor="w", pady=(0, 8))

    description = metadata.description
    height = min(12, max(4, description.count('\n') + len(description) // 60 + 1))
    text_box = ttk.Frame(frame)
    text_box.pack(fill="both", expand=True)
    text_scroll = ttk.Scrollbar(text_box, orient="vertical")
    text_scroll.pack(side="right", fill="y")
    text = tk.Text(text_box, wrap="word", width=56, height=height,
                   background=CELL, foreground=FG, relief="flat",
                   borderwidth=0, padx=8, pady=8, highlightthickness=1,
                   highlightbackground=BORDER, highlightcolor=BORDER,
                   font=base_font, cursor="arrow",
                   yscrollcommand=text_scroll.set)
    text_scroll.configure(command=text.yview)
    text.insert("1.0", description)
    text.configure(state="disabled")
    text.pack(side="left", fill="both", expand=True)

    ttk.Button(frame, text="Close", command=close_popup).pack(anchor="e", pady=(12, 0))

    popup.bind("<Escape>", lambda e: close_popup())
    popup.protocol("WM_DELETE_WINDOW", close_popup)

    popup.update_idletasks()
    x = min(max(event.x_root - 60, 0), root.winfo_screenwidth() - popup.winfo_width() - 20)
    y = min(max(event.y_root - 40, 0), root.winfo_screenheight() - popup.winfo_height() - 40)
    popup.geometry(f"+{x}+{y}")
    popup.grab_set()
    popup.focus_set()


# ------------------------------------------------------------- bindings ---

def search_has_focus():
    try:
        return root.focus_get() is search_entry
    except KeyError:
        return False


def on_key_up(event):
    if search_has_focus():
        return
    move_selection(-1)
    return "break"


def on_key_down(event):
    if search_has_focus():
        return
    move_selection(1)
    return "break"


def on_key_enter(event):
    if search_has_focus():
        return
    if selected_key is not None:
        toggle_play(selected_key, toggle=True)
    return "break"


def on_key_escape(event=None):
    if popup is not None and popup.winfo_exists():
        close_popup()
        return "break"
    if search_var.get():
        search_var.set("")
        return "break"


def on_key_left(event):
    if search_has_focus():
        return
    if view_start is not None:
        page_view(-SLOT_SECONDS)
    return "break"


def on_key_right(event):
    if search_has_focus():
        return
    if view_start is not None:
        page_view(SLOT_SECONDS)
    return "break"


def on_wheel(event):
    if popup is not None and event.widget.winfo_toplevel() is popup:
        return
    if event.num == 4 or getattr(event, 'delta', 0) > 0:
        units = -2
    elif event.num == 5 or getattr(event, 'delta', 0) < 0:
        units = 2
    else:
        return "break"
    canvas.yview_scroll(units, "units")
    return "break"


def on_canvas_configure(event):
    canvas.itemconfigure(canvas_window, width=event.width)
    update_nowline()


def on_close():
    if player_process is not None and player_process.poll() is None:
        try:
            mpv_command({'command': ['quit']})
        except OSError:
            pass
    kill_player()
    root.destroy()


search_var.trace_add("write", lambda *args: apply_filter(reset_scroll=True))
search_entry.bind("<Escape>", on_key_escape)
epg_only_check.configure(command=lambda: apply_filter(reset_scroll=True))

prev_button.configure(command=lambda: page_view(-SLOT_SECONDS))
next_button.configure(command=lambda: page_view(SLOT_SECONDS))
now_button.configure(command=jump_to_now)

root.bind("<Up>", on_key_up)
root.bind("<Down>", on_key_down)
root.bind("<Left>", on_key_left)
root.bind("<Right>", on_key_right)
root.bind("<Return>", on_key_enter)
root.bind("<KP_Enter>", on_key_enter)
root.bind("<Escape>", on_key_escape)
root.bind("<Prior>", lambda e: canvas.yview_scroll(-8, "units"))
root.bind("<Next>", lambda e: canvas.yview_scroll(8, "units"))

for event_name in ("<Button-4>", "<Button-5>", "<MouseWheel>"):
    root.bind_all(event_name, on_wheel)

canvas.bind("<Configure>", on_canvas_configure)
scroll_frame.bind("<Configure>", framesync)
root.protocol("WM_DELETE_WINDOW", on_close)


# ----------------------------------------------------------------- loop ---

def minute_update():
    global now
    now = int(time.time())

    if follow_now:
        start = time_window_start(now)
        if start != view_start or not server_ok:
            set_view(start, follow=True)
        else:
            update_header()
    else:
        update_header()
        if not server_ok:
            update_epg_grid()

    clock_label.configure(text=thm(now))
    date_label.configure(text=thd(now))
    update_nowline()

    # wait until the next minute
    t = time.time()
    nxt = ((t // 60) + 1) * 60
    delta = int((nxt - t) * 1000) + 10
    root.after(delta, minute_update)


def wakeup_poll():
    root.after(200, wakeup_poll)


wakeup_poll()
minute_update()

root.mainloop()
