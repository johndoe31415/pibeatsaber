#	pibeatsaber - Beat Saber historian application that tracks players
#	Copyright (C) 2019-2019 Johannes Bauer
#
#	This file is part of pibeatsaber.
#
#	pibeatsaber is free software; you can redistribute it and/or modify
#	it under the terms of the GNU General Public License as published by
#	the Free Software Foundation; this program is ONLY licensed under
#	version 3 of the License, later versions are explicitly excluded.
#
#	pibeatsaber is distributed in the hope that it will be useful,
#	but WITHOUT ANY WARRANTY; without even the implied warranty of
#	MERCHANTABILITY or FITNESS FOR A PARTICULAR PURPOSE.  See the
#	GNU General Public License for more details.
#
#	You should have received a copy of the GNU General Public License
#	along with this program.  If not, see <https://www.gnu.org/licenses/>.
#
#	Johannes Bauer <JohannesBauer@gmx.de>

import os
import socket
import asyncio
import websockets
import json
import time
import datetime
import contextlib
import gzip
from ScoreKeeper import ScoreKeeper

class BeatSaberHistorian():
	def __init__(self, config, args):
		self._config = config
		self._args = args
		self._current_player = None
		self._current_data = None
		self._connected_to_beatsaber = False
		self._current_score = None
		self._last_score = None
		self._score_change = asyncio.Event()

#	def _subs(self, text):
#		text = text.replace("${player}", self._current_player or "unknown_player")
#		return self._config.subs(text)

	def _get_status_dict(self):
		return {
			"connection": {
				"connected_to_beatsaber":	self._connected_to_beatsaber,
				"current_player":			self._current_player,
				"in_game":					self._current_data is not None,
			},
			"current_game":					self._current_score.to_dict() if (self._current_score is not None) else None,
			"last_game":					self._last_score.to_dict() if (self._last_score is not None) else None,
		}

	def _local_command_status(self, query):
		return {
			"msgtype":	"response",
			"success":	True,
			"data": self._get_status_dict(),
		}

	def _local_command_set(self, query):
		if ("current_player" in query) and (isinstance(query["current_player"], (type(None), str))):
			self._current_player = query["current_player"]
			print("Current player is now %s" % (self._current_player))
		return self._local_command_status(query)

	def _process_local_command(self, query):
		if not isinstance(query, dict):
			return {
				"success":	False,
				"data":		"Invalid data type provided, expected dict.",
			}

		cmd = query.get("cmd", "")
		handler = getattr(self, "_local_command_%s" % (cmd), None)
		if handler is not None:
			return handler(query)
		else:
			return {
				"msgtype":	"response",
				"success":	False,
				"data":		"No such command: \"%s\"" % (cmd),
			}

	async def _local_server_commands(self, reader, writer):
		try:
			while not writer.is_closing():
				msg = await reader.readline()
				if len(msg) == 0:
					break
				try:
					msg = json.loads(msg)
					response = self._process_local_command(msg)
					writer.write((json.dumps(response) + "\n").encode("ascii"))
				except json.decoder.JSONDecodeError as e:
					writer.write((json.dumps({
						"msgtype":	"response",
						"success":	False,
						"data":		"Could not decode command: %s" % (str(e)),
					}) + "\n").encode("ascii"))
		except (ConnectionResetError, BrokenPipeError) as e:
			print("Local UNIX server caught exception:", e)
			writer.close()

	async def _local_server_events(self, reader, writer):
		self._score_change.set()
		while not writer.is_closing():
			await self._score_change.wait()
			self._score_change.clear()
			writer.write((json.dumps({
				"msgtype":	"event",
				"status": self._get_status_dict(),
			}) + "\n").encode("ascii"))

	async def _local_server_tasks(self, reader, writer):
		await asyncio.gather(
			self._local_server_commands(reader, writer),
			self._local_server_events(reader, writer),
		)
		writer.close()

	async def _create_local_server(self):
		await asyncio.start_unix_server(self._local_server_tasks, path = self._config["unix_socket"])

	def _finish_song(self):
		now = datetime.datetime.now().strftime("%Y_%m_%d_%H_%M_%S")
		destination_filename = self._config["history_directory"] + "/" + (self._current_player if (self._current_player is not None) else "unknown_player") + "/" + now + ".json.gz"
		with contextlib.suppress(FileExistsError):
			os.makedirs(os.path.dirname(destination_filename))
		with gzip.open(destination_filename, "wt") as f:
			json.dump(self._current_data, f)
			f.write("\n")
		self._current_data = None

	def _handle_beatsaber_event(self, event):
		if event["event"] == "songStart":
			self._current_score = ScoreKeeper()
			self._current_data = {
				"meta": {
					"songStartLocal":	time.time(),
					"player":			self._current_player,
				},
				"events":		[ ],
			}
			self._current_data["events"].append(event)
			print("Player %s started %s - %s (%s)" % (self._current_player, event["status"]["beatmap"]["songAuthorName"], event["status"]["beatmap"]["songName"], event["status"]["beatmap"]["difficulty"]))
		elif (self._current_data is not None) and ((event["event"] == "finished") or (event["event"] == "failed")):
			self._current_data["events"].append(event)
			self._last_score = self._current_score
			self._current_score = None
			self._finish_song()
		elif self._current_data is not None:
			self._current_data["events"].append(event)
		if self._current_score is not None:
			self._current_score.process(event)
			self._score_change.set()

	async def _connect_beatsaber(self):
		uri = self._config["beatsaber_websocket_uri"]
		while True:
			try:
				async with websockets.connect(uri) as websocket:
					print("Connection to BeatSaber established at %s" % (uri))
					self._connected_to_beatsaber = True
					while True:
						msg = await websocket.recv()
						msg = json.loads(msg)
						self._handle_beatsaber_event(msg)
			except (OSError, ConnectionRefusedError, websockets.exceptions.ConnectionClosed):
				self._connected_to_beatsaber = False
				await asyncio.sleep(1)

	async def _connect_heartrate_monitor(self):
		socket = self._config["heartrate_monitor"]
		while True:
			await asyncio.sleep(1)

	def start(self):
		loop = asyncio.get_event_loop()
		loop.create_task(self._create_local_server())
		loop.create_task(self._connect_beatsaber())
		if self._config.has("heartrate_monitor"):
			loop.create_task(self._connect_heartrate_monitor())
		try:
			loop.run_forever()
		except KeyboardInterrupt:
			with contextlib.suppress(FileNotFoundError):
				os.unlink(self._config["unix_socket"])
