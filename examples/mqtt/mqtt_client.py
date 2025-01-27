"""MQTT client for PET-7018Z."""

import argparse
import collections
import json
import queue
import pathlib
import threading
import time
import traceback
import uuid
import sys

import paho.mqtt.client as mqtt
import paho.mqtt.publish as publish

from mqtt_tools.queue_publisher import MQTTQueuePublisher

sys.path.insert(1, str(pathlib.Path.cwd().parent.parent.joinpath("src")))
import xet7019z.xet7019z as xet7019z

parser = argparse.ArgumentParser()
parser.add_argument(
    "-mqtthost",
    type=str,
    default="127.0.0.1",
    help="IP address or hostname for MQTT broker.",
)

args = parser.parse_args()

# start queue publisher
mqttqp = MQTTQueuePublisher()
mqttqp.connect(args.mqtthost)
mqttqp.loop_start()

# hold start/stop condition in thread-safe container
start = collections.deque(maxlen=1)
start.append(False)

# container for config data sent over mqtt
config = {}

# create queue for worker tasks
q = queue.Queue()

# inti the instrument object
daq = xet7019z()


def worker():
    """Work on requests sent over MQTT."""
    while True:
        msg = q.get()

        payload = json.loads(msg.payload)

        print(payload)

        # handle continuous start/stop
        if msg.topic == "daq/start":
            start.append(True)
            print("Starting continuous mode...")
        elif msg.topic == "daq/stop":
            start.append(False)
            print("Continuous mode stopped.")
        elif msg.topic == "measurement/log":
            if payload["msg"] == "Run complete!" or payload["msg"].startswith(
                "RUN ABORTED!"
            ):
                # make sure continuous mode stops
                start.append(False)

                # wait for measurement delay + 1s to ensure last measurement
                # finishes
                time.sleep(config["daq"]["delay"] + 1)
                print(payload["msg"])
        elif msg.topic == "daq/single":
            if start[0] is False:
                single()
            else:
                log(
                    "Cannot run single measurement: DAQ running in continuous mode.", 30
                )
        elif msg.topic == "measurement/run":
            if start[0] is False:
                print("Received run message")
                read_config(payload)
                setup()
            else:
                log("Cannot update config/setup: DAQ running in continuous mode.", 30)

        q.task_done()


def continuous():
    """Measure data in continuous mode.

    This function runs in its own thread.
    """
    while True:
        if start[0] is True:
            single()
            time.sleep(config["daq"]["delay"])
        else:
            time.sleep(1)


def single():
    """Perform single shot measurement."""
    data = [time.time()]
    for channel in config["daq"]["channels"].keys():
        data.extend([daq.measure(channel)])
    handle_data(data)


def handle_data(data):
    """Handle measurement data.

    Parameters
    ----------
    data : array-like
        Measurement data.
    """
    payload = {
        "data": data,
        "pixel": {},
        "sweep": "",
    }
    mqttqp.append_payload("data/raw/daq", json.dumps(payload))


def log(msg, level):
    """Publish info for logging.

    Parameters
    ----------
    msg : str
        Log message.
    level : int
        Log level used by logging module:

            * 50 : CRITICAL
            * 40 : ERROR
            * 30 : WARNING
            * 20 : INFO
            * 10 : DEBUG
            * 0 : NOTSET
    """
    payload = {"level": level, "msg": msg}
    mqttqp.append_payload("measurement/log", json.dumps(payload))


def read_config(payload):
    """Get config data from payload.

    Parameters
    ----------
    payload : dict
        Request dictionary for measurement server.
    """
    global config
    config = payload["config"]


def setup():
    """Set up the instrument for measurements."""
    try:
        if daq.instr is None:
            daq.connect(
                config["daq"]["host"],
                config["daq"]["port"],
                config["daq"]["timeout"],
                True,
            )

        print(f"Connected to '{daq.get_id()}'!")

        # disable all analog inputs
        for channel in range(10):
            daq.enable_ai(channel, False)

        # global settings
        daq.set_ai_noise_filter(config["daq"]["plf"])
        daq.enable_cjc(True)

        # setup and enable the analog inputs in use
        for channel, ai_range in config["daq"]["channels"].items():
            daq.set_ai_range(channel, ai_range)
            daq.enable_ai(channel, True)
    except Exception as e:
        traceback.print_exc()
        log("DAQ setup failed! " + str(e), 40)


def on_message(mqttc, obj, msg):
    """Act on an MQTT msg."""
    q.put_nowait(msg)


# start thread for managing queue tasks
threading.Thread(target=worker, daemon=True).start()

# start thread for measurement tasks
threading.Thread(target=continuous, daemon=True).start()

# create mqtt client
client_id = f"daq-{uuid.uuid4().hex}"
mqttc = mqtt.Client(client_id)
mqttc.will_set("daq/status", json.dumps(f"{client_id} offline"), 2, retain=True)
mqttc.on_message = on_message
mqttc.connect(args.mqtthost)
mqttc.subscribe("measurement/#", qos=2)
mqttc.subscribe("daq/#", qos=2)
publish.single(
    "daq/status",
    json.dumps(f"{client_id} ready"),
    qos=2,
    hostname=args.mqtthost,
)
print(f"{client_id} connected!")
mqttc.loop_forever()
