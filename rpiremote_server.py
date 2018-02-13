#!/usr/bin/env python
# RS_Server.py - Web Server Class for the Raspberry Pi
#
# Based on server.py from pistreaming
# ref: https://github.com/waveform80/pistreaming
# Copyright 2014 Dave Hughes <dave@waveform.org.uk>
#
# 06 March 2017 - 1.0 Original Issue
#
# Reefwing Software
# Simplified BSD Licence - see bottom of file.

import sys, io, os, shutil, picamera, signal

from subprocess import Popen, PIPE, check_output
from string import Template
from struct import Struct
from threading import Thread
from time import sleep, time
from http.server import HTTPServer, BaseHTTPRequestHandler
from wsgiref.simple_server import make_server
from ws4py.websocket import WebSocket
from ws4py.server.wsgirefserver import WSGIServer, WebSocketWSGIRequestHandler
from ws4py.server.wsgiutils import WebSocketWSGIApplication

###########################################
# CONFIGURATION
WIDTH = 640
HEIGHT = 480
FRAMERATE = 24
HTTP_PORT = 8082
WS_PORT = 8084
COLOR = u'#444'
BGCOLOR = u'#333'
JSMPEG_MAGIC = b'jsmp'
JSMPEG_HEADER = Struct('>4sHH')
###########################################


class StreamingHttpHandler(BaseHTTPRequestHandler):
    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        if self.path == '/':
            self.send_response(301)
            self.send_header('Location', '/index.html')
            self.end_headers()
            return
        elif self.path == '/jsmpg.js':
            content_type = 'application/javascript'
            content = self.server.jsmpg_content
        elif self.path == '/index.html':
            content_type = 'text/html; charset=utf-8'
            tpl = Template(self.server.index_template)
            content = tpl.safe_substitute(dict(
                ADDRESS='%s:%d' % (self.request.getsockname()[0], WS_PORT),
                WIDTH=WIDTH, HEIGHT=HEIGHT, COLOR=COLOR, BGCOLOR=BGCOLOR))
        else:
            self.send_error(404, 'File not found')
            return
        content = content.encode('utf-8')
        self.send_response(200)
        self.send_header('Content-Type', content_type)
        self.send_header('Content-Length', len(content))
        self.send_header('Last-Modified', self.date_time_string(time()))
        self.end_headers()
        if self.command == 'GET':
            self.wfile.write(content)


class StreamingHttpServer(HTTPServer):
    def __init__(self):
        super(StreamingHttpServer, self).__init__(
                ('', HTTP_PORT), StreamingHttpHandler)
        with io.open('index.html', 'r') as f:
            self.index_template = f.read()
        with io.open('jsmpg.js', 'r') as f:
            self.jsmpg_content = f.read()


class StreamingWebSocket(WebSocket):
    def opened(self):
        self.send(JSMPEG_HEADER.pack(JSMPEG_MAGIC, WIDTH, HEIGHT), binary=True)


class BroadcastOutput(object):
    def __init__(self, camera):
        print('Spawning background conversion process')
        self.converter = Popen([
            'avconv',
            '-f', 'rawvideo',
            '-pix_fmt', 'yuv420p',
            '-s', '%dx%d' % camera.resolution,
            '-r', str(float(camera.framerate)),
            '-i', '-',
            '-f', 'mpeg1video',
            '-b', '800k',
            '-r', str(float(camera.framerate)),
            '-'],
            stdin=PIPE, stdout=PIPE, stderr=io.open(os.devnull, 'wb'),
            shell=False, close_fds=True)

    def write(self, b):
        self.converter.stdin.write(b)

    def flush(self):
        print('Waiting for background conversion process to exit')
        self.converter.stdin.close()
        self.converter.wait()


class BroadcastThread(Thread):
    def __init__(self, converter, websocket_server):
        super(BroadcastThread, self).__init__()
        self.converter = converter
        self.websocket_server = websocket_server

    def run(self):
        try:
            while True:
                buf = self.converter.stdout.read(512)
                if buf:
                    self.websocket_server.manager.broadcast(buf, binary=True)
                elif self.converter.poll() is not None:
                    break
        finally:
            self.converter.stdout.close()

class Server():
    def __init__(self):
        # Create a new server instance
        print("Initializing camera")
        self.camera = picamera.PiCamera()
        self.camera.resolution = (WIDTH, HEIGHT)
        self.camera.framerate = FRAMERATE
        # hflip and vflip depends on how you mount the camera
        self.camera.vflip = True
        self.camera.hflip = False 
        sleep(1) # camera warm-up time
        print("Camera ready")

    def __str__(self):
        # Return string representation of server
        ip_addr = check_output(['hostname', '-I']).decode().strip()
        return "Server video stream at http://{}:{}".format(ip_addr, HTTP_PORT)

    def start(self):
        # Start video server streaming
        print('Initializing websockets server on port %d' % WS_PORT)
        self.websocket_server = make_server(
            '', WS_PORT,
            server_class=WSGIServer,
            handler_class=WebSocketWSGIRequestHandler,
            app=WebSocketWSGIApplication(handler_cls=StreamingWebSocket))
        self.websocket_server.initialize_websockets_manager()
        self.websocket_thread = Thread(target=self.websocket_server.serve_forever)
        print('Initializing HTTP server on port %d' % HTTP_PORT)
        self.http_server = StreamingHttpServer()
        self.http_thread = Thread(target=self.http_server.serve_forever)
        print('Initializing broadcast thread')
        output = BroadcastOutput(self.camera)
        self.broadcast_thread = BroadcastThread(output.converter, self.websocket_server)
        print('Starting recording')
        self.camera.start_recording(output, 'yuv')
        print('Starting websockets thread')
        self.websocket_thread.start()
        print('Starting HTTP server thread')
        self.http_thread.start()
        print('Starting broadcast thread')
        self.broadcast_thread.start()
        print("Video Stream available...")
        while True:
            self.camera.wait_recording(1)

    def cleanup(self):
        # Stop video server - close browser tab before calling cleanup
        print('Stopping recording')
        self.camera.stop_recording()
        print('Waiting for broadcast thread to finish')
        self.broadcast_thread.join()
        print('Shutting down HTTP server')
        self.http_server.shutdown()
        print('Shutting down websockets server')
        self.websocket_server.shutdown()
        print('Waiting for HTTP server thread to finish')
        self.http_thread.join()
        print('Waiting for websockets thread to finish')
        self.websocket_thread.join()

def main():
    server = Server()
    print(server)

    def endProcess(signum = None, frame = None):
        # Called on process termination. 
        if signum is not None:
            SIGNAL_NAMES_DICT = dict((getattr(signal, n), n) for n in dir(signal) if n.startswith('SIG') and '_' not in n )
            print("signal {} received by process with PID {}".format(SIGNAL_NAMES_DICT[signum], os.getpid()))
        print("\n-- Terminating program --")
        print("Cleaning up Server...")
        server.cleanup()
        print("Done.")
        exit(0)

    # Assign handler for process exit
    signal.signal(signal.SIGTERM, endProcess)
    signal.signal(signal.SIGINT, endProcess)
    signal.signal(signal.SIGHUP, endProcess)
    signal.signal(signal.SIGQUIT, endProcess)
    
    server.start()
    
            
if __name__ == '__main__':
    main()
