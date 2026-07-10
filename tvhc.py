#!/usr/bin/python

import sys,configparser,os, requests, collections, time, pprint, subprocess
from types import SimpleNamespace

config = configparser.ConfigParser(allow_unnamed_section=True)
config.read(os.path.expanduser('~/.tvhc.ini'))
config = config[configparser.UNNAMED_SECTION]

if len(sys.argv) > 1:
    reqAuth = None
    TVHE_Server = sys.argv[1]
else:
    proto = "https" if config.getboolean('UseTLS', False) else "http"
    TVHE_Server = f"{proto}://{config.get('Server')}/"
    username = config.get('Username','')
    password = config.get('Password','')
    a = config.get('Auth','Digest' if username or password else 'None')
    if a == 'None':
        reqAuth = None
    elif a == 'Digest':
        reqAuth = requests.auth.HTTPDigestAuth(username,password)
    elif a == 'Basic' or a == 'Plain':
        reqAuth = requests.auth.HTTPBasicAuth(username,password)
    else:
        print('Unrecognized Auth value "{a}" in config.')
        sys.exit(1)

def requests_get(*args, **kwargs):
    kwargs['auth'] = reqAuth
    try:
        resp = requests.get(*args, **kwargs)
        resp.raise_for_status()
        return resp
    except requests.exceptions.RequestException as err:
        raise SystemExit(err)

# number of half-hour time slots to show in scrollable grid
nslots = 6

def start_player(uuid, title):
    response = requests_get(f'{TVHE_Server}/play/ticket/stream/channel/{uuid}', headers={'User-Agent': 'ticket-please'})
    urls = [line for line in response.text.splitlines() if (line and not line.startswith('#'))]
    print(f'starting player for {urls}')
    return subprocess.Popen(['mpv', '--quiet', f'--title={title}'] + urls)

# fetch epg data from TVH server
def get_epg_data(fstart=None, fend=None):
    response = requests_get(f'{TVHE_Server}/api/epg/events/grid?limit=99999')
    epg = response.json()

    # lists of program events using chanel num,name as key
    channels = collections.defaultdict(list)

    # Filter out events that are outise grid start/end times.
    # Events are come from server sorted by start time, so when we
    # find one with start after end of grid, then we're done.

    for e in epg['entries']:
        if fstart and e['stop'] < fstart:
            continue
        if fend and e['start'] > fend:
            break
        key = e['channelNumber'],e['channelName']
        channels[key].append(e)

    # sort by channel as FP number so that 9.9 comes before 10.1
    return dict(sorted(channels.items(), key = lambda item: (float(item[0][0]),item[0][1])))


import tkinter as tk
from tkinter import ttk
from tkinter import font

root = tk.Tk()

style = ttk.Style()
style.theme_use("clam")

# bold and reverse+bold label styles
default_label_font = font.nametofont("TkDefaultFont").copy()
default_label_font.config(weight="bold")
style.configure("Bold.TLabel", font=default_label_font)
style.configure("Reverse.Bold.TLabel", foreground = style.lookup("Bold.TLabel", "background"), background = style.lookup("Bold.TLabel", "foreground"))

root.title("TVHC — TVHeadend Client")
root.geometry("900x600")

# main container frame
container = ttk.Frame(root)
container.pack(fill="both", expand=True, padx=10, pady=10)

# header elements at top of the container frame
hdrLeft = ttk.Frame(container)  # above scroll bar
hdrLeft.grid(row=0, column=0)
hdrRight = ttk.Frame(container) # above scrollable grid
hdrRight.grid(row=0, column=1, sticky="nsew")
hdrRight.grid_columnconfigure(1, weight=1)
hdrRight0 = ttk.Label(hdrRight, anchor="n")  # above channel num/name where we show time
hdrRight0.grid(row=0, column=0, sticky="nsew")
hdrRight1 = ttk.Frame(hdrRight)              # above program info where we show slot lables and time progress bar
hdrRight1.grid(row=0, column=1, sticky="nsew")

def update_headertime():
    global now
    hdrRight0.config(text=time.strftime("%-I:%M", time.localtime(now)))

# Canvas on right (contans channel/program info) inside the container
canvas = tk.Canvas(container, highlightthickness=0)
canvas.grid(row=1, column=1, sticky="nsew")

# Scrollbar on left
scrollbar = ttk.Scrollbar(container, orient="vertical", command=canvas.yview)
scrollbar.grid(row=1, column=0, sticky="ns")
canvas.configure(yscrollcommand=scrollbar.set)

container.rowconfigure(1, weight=1)
container.columnconfigure(1, weight=1)

scrollable_frame = ttk.Frame(canvas)
scrollable_frame.grid_columnconfigure(1, weight=1)

canvas_window = canvas.create_window((0, 0), window=scrollable_frame, anchor="nw")
def fix_width(event):
    canvas.itemconfig(canvas_window, width=event.width)
canvas.bind("<Configure>", fix_width)

# popup showing program details when program label is left-clicked
def program_popup(event):
    w = event.widget

    wrapwidth=400
    popup = tk.Toplevel(root)
    popup.overrideredirect(True)
    popup.geometry(f"+{event.x_root-(wrapwidth//2)}+{event.y_root-40}")

    frame = ttk.Frame(popup, borderwidth=5, relief="raised", padding=20)
    title = ttk.Label(frame, text= w['text'],style="Bold.TLabel")
    descr = ttk.Label(frame, text=w.metadata.description, wraplength=400)
    d = w.metadata.length
    tstr = f'start: {thm(w.metadata.start)}    len: {round(d/60)}m'
    times = ttk.Label(frame, text=tstr, anchor='e')

    frame.pack()
    title.pack()
    descr.pack(expand=True)
    times.pack(fill="x")

    for w in frame, popup, title, descr:
        w.bind("<Button-1>", lambda event: popup.destroy())
        w.bind("<Button-2>", lambda event: popup.destroy())
        w.bind("<Button-3>", lambda event: popup.destroy())

    popup.wait_visibility()
    popup.grab_set()

currently_playing = None  # channel widget that's "active"
player_process = None     # subprocess object of running video player

def player_exited():
    print("player exited")
    player_process = None
    if currently_playing:
        stop(currently_playing)

def handle_sigchild(signum, frame):
    print("sigchild")
    if player_process and player_process.poll() is not None:
        root.after(10, player_exited)

import signal
signal.signal(signal.SIGCHLD, handle_sigchild)

def play(w):
    global currently_playing, player_process
    if w is None or w is currently_playing:
        return
    print(f"play {w['text']} {w.metadata.uuid}")
    w.configure(style="Reverse.Bold.TLabel")
    currently_playing = w
    player_process = start_player(w.metadata.uuid,w['text'])

def stop(w):
    global currently_playing, player_process
    if player_process:
        player_process.terminate()
        player_process = None
    if w is None or w is not currently_playing:
        return
    print(f"stop {w['text']} {w.metadata.uuid}")
    w.configure(style="Bold.TLabel")
    currently_playing = None

def play_stop(event):
    chanwidget = getattr(event.widget.metadata, 'chanwidget', event.widget)
    global currently_playing
    if event.widget is currently_playing:
        stop(currently_playing)
    else:
        stop(currently_playing)
        play(chanwidget)

# somewhere to store the channel widget and the frame that contains
# the program labels for each row in the epg grid.  Key is
# (channum,channame) tuple, same as the dict we fetch from server.
gridrows = {}

# fetch epg data and fill in scrollable grid with channel/program info
def update_epg_grid():
    global now, nslots, gridstart, gridend

    epg = get_epg_data(gridstart, gridend)

    if not gridrows:
        # Must be first time. For each row, create channel label
        # widget and empty frame for programs
        for index,key in enumerate(epg):
            # label for channel
            chnum,chname = key
            chan = ttk.Label(scrollable_frame, text=f"{chnum} {chname}", borderwidth=1, relief="solid", padding=5,style="Bold.TLabel")
            chan.grid(row=index, column=0, sticky="nsew", padx=0, pady=0)
            chan.metadata = SimpleNamespace(uuid=epg[key][0]['channelUuid'])  # channel UUID from first event
            chan.bind('<Button-1>', play_stop)
            # frame in which to put programs
            progs = ttk.Frame(scrollable_frame)
            progs.grid(row=index, column=1, sticky="nsew", padx=0, pady=0)
            gridrows[key] = SimpleNamespace(chan=chan,progs=progs)

    # delete old program labels (if any)
    for row in gridrows.values():
        for child in row.progs.winfo_children():
            child.destroy()

    # add new program labels
    for key,events in epg.items():
        row = gridrows[key]
        col = 0
        proggrp = f'proggroup{key}' # ID for uniform weighting of each row
        tt = gridstart  # where we are in the timeline between grid start/end

        for e in events:
            title = e['title']
            start = e['start']
            stop =  e['stop']

            if start < tt:
                start = tt
            if stop > gridend:
                stop = gridend

            if start > tt:
                # there's a gap to be filled before next program
                duration = start-tt
                f = ttk.Frame(row.progs)
                f.grid(row=0,column=col,sticky="nsew")
                row.progs.columnconfigure(col, weight=duration, uniform=proggrp)
                tt += duration
                col += 1

            duration = stop-start
            # some stations schedule very small programs (e.g. 1
            # second) and very small weights trigger Xlib fault
            if duration < 10:
                continue
            label = ttk.Label(row.progs, text=title, borderwidth=1, relief="solid", padding=5)
            label.metadata = SimpleNamespace(chanwidget=row.chan,
                                             title=title,
                                             description=e.get('description','no description available'),
                                             start=e['start'],
                                             stop=e['stop'],
                                             length=e['stop']-e['start'])
            label.bind('<Button-1>', play_stop)
            label.bind("<Button-3>", program_popup)
            label.grid(row=0,column=col,sticky="nsew")
            row.progs.columnconfigure(col, weight=duration, uniform=proggrp)
            tt += duration
            col += 1

        if tt < gridend:
            # gap to be filled at end
            duration = gridend - tt
            f = ttk.Frame(row.progs)
            f.grid(row=0,column=col,sticky="nsew")
            row.progs.columnconfigure(col, weight=duration, uniform=proggrp)
            tt += duration
            col += 1


# col 0 in the header grid needs to be same width as col 0 in the
# scrollable grid so that things line up. When scroll grid changes
# size, update the scroll bar, and then read the width of col 0 and
# set hdr col 0 to match.
def framesync(event):
    canvas.configure(scrollregion=canvas.bbox("all"))
    cwidth = scrollable_frame.grid_bbox(0,0)[2]
    hdrRight.columnconfigure(0, minsize=cwidth, weight=0)
scrollable_frame.bind("<Configure>", framesync)

# convert a time value to HH:MM or HH:MM:SS string
def thm(t):
    return time.strftime("%-I:%M", time.localtime(t))

def thms(t):
    return time.strftime("%-I:%M:%S", time.localtime(t))

# time/progress bar in upper half of header above prog info
style.configure("tbar.TFrame", background="darkgray")
tbar = ttk.Frame(hdrRight1,  borderwidth=1, relief="solid")
tbar.grid(row=0,column=0, columnspan=nslots, sticky="nsew")
tbleft = ttk.Frame(tbar, style="tbar.TFrame")
tbleft.grid(row=0, column=0, sticky="nsew")
tbright = ttk.Frame(tbar)
tbright.grid(row=0, column=1, sticky="nsew")
tbar.columnconfigure(0, uniform="tbar")
tbar.columnconfigure(1, uniform="tbar")
tbar.rowconfigure(0, minsize=7)

# 30min slot labels in lower half of header above prog info
slot_header_labels = []
for i in range(nslots):
    slabel = ttk.Label(hdrRight1, borderwidth=1, relief="solid")
    slabel.grid(row=1,column=i, sticky="nsew")
    hdrRight1.columnconfigure(i, weight=30, uniform="slabels")
    slot_header_labels.append(slabel)

# globals to keep track of current time and grid start/end (in
# seconds since epoch)
now = None
gridstart = None
gridend = None

# update the timeslot labels
def update_epg_header():
    global gridstart
    for i in range(nslots):
        text=thm(gridstart + i*30*60)
        slot_header_labels[i].configure(text=text)

# update the time/progress bar
def update_tbar():
    tbar.columnconfigure(0, weight=now-gridstart)
    tbar.columnconfigure(1, weight=gridend - now)

# update epg grid if start time for grid is changing
def update_grid():
    global gridstart, gridend, now
    nstart = ((now - 31*60) // (30*60)) * (30*60)
    if nstart != gridstart:
        gridstart = nstart
        gridend = gridstart + (nslots * 30*60)
        update_epg_grid()
        update_epg_header()

# runs at the top of every minute to update widgets
def minuteUpdate():
    global now
    now = int(time.time())
    update_grid()
    update_tbar()
    update_headertime()

    # wait until next minute
    t = time.time()
    next = ((t//60)+1) * 60
    delta = int((next-t)*1000)+10
    root.after(delta, minuteUpdate)

# wakeup more frequently just to receive signals
def wakeupPoll():
    root.after(200, wakeupPoll)

wakeupPoll()
minuteUpdate()

root.mainloop()
 
