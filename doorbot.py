#!/usr/bin/env python3

import datetime
import re
from settings import *
import signal
import sys
import threading
from threading import Thread
from time import sleep
from queue import Empty, Queue

import RPi.GPIO as GPIO


# GPIO 0-8 have default pull-ups
# GPIO 9-27 have default pull-downs
INDICATOR_LED_INPUT_GPIO = 6
DOORBELL_BUTTON_INPUT_GPIO = 26

KEY_BUTTON_OUTPUT_GPIO = 21
SERVO_POWER_ENABLE_OUTPUT_GPIO = 20
SERVO_PWM_OUTPUT_GPIO = 5
ERT_CONTACTS_OUTPUT_GPIO = 19
HEARTBEAT_OUTPUT_GPIO = 25
BUZZER_OUTPUT_GPIO = 16

MIN_BLINK_PAUSE = 3  # seconds   A blink after at least that time starts a new blinking (phone blinks every 1.2s)
KEY_BUTTON_PRESS_AT_BLINK_COUNT = 4  # =~ 3.6s
KEY_BUTTON_PRESS_AT_BLINK_COUNT_DELAYED = 35  # up to 37
KEY_BUTTON_PRESS_DURATION = 5  # seconds, should be < MIN_BLINK_PAUSE + (KEY_BUTTON_PRESS_AT_BLINK_COUNT - 1) * 1.2

# TODO Make it adaptive
SHORT_PRESS_MAX = datetime.timedelta(seconds=0.9)
LONG_PRESS_MIN = datetime.timedelta(seconds=1.6)

# Shorter for single press to not delay the doorbell after a single press
SINGLE_PRESS_VALID_SEQUENCE_PROCESSING_DELAY = datetime.timedelta(seconds=1)
MULTIPLE_PRESS_VALID_SEQUENCE_PROCESSING_DELAY = datetime.timedelta(seconds=2)

INVALID_PRESS_SEQUENCE_CLEAR_TIMEOUT = datetime.timedelta(seconds=6)  # also max recognizable long press duration

# Do not ring the doorbell if button pressed more times than this setting.
# If there are more, the user is most probably trying to enter the code and does not want to ring the bell.
DOOR_OPEN_BUTTON_DOORBELL_SOUND_MAX_PRESSES = 2

ERT_CONTACTS_CONNECT_DURATION = 0.4  # s  Duration of simulated doorbell button press

DOOR_HANDLE_MOVEMENT_DURATION = 2  # s

SERVO_PULSE_FREQUENCY = 50  # Hz
DOOR_HANDLE_DOWN_SERVO_PULSE_LENGTH = 2200  # us
DOOR_HANDLE_UP_SERVO_PULSE_LENGTH = 800  # us

GPIO_EVENT_DOUBLECHECK_DELAY = 0.03  # s
BUSY_WAIT_SLEEP_DURATION = 0.05  # s


def format_time(time):
    return time.strftime("%d-%m-%Y %H:%M:%S.%f")


def log(string):
    print(f"{format_time(datetime.datetime.now())}: {string}")


def key_button_loop():
    global threads_should_run
    global should_press_key_button

    while threads_should_run:
        if should_press_key_button:
            should_press_key_button = False
            GPIO.output(KEY_BUTTON_OUTPUT_GPIO, GPIO.HIGH)
            log("key button down")
            sleep(KEY_BUTTON_PRESS_DURATION)
            GPIO.output(KEY_BUTTON_OUTPUT_GPIO, GPIO.LOW)
            # log("key button up")

        sleep(BUSY_WAIT_SLEEP_DURATION)


def doorbell_button_press_processor_loop():
    global doorbell_button_event_queue
    global should_open_door
    global should_ring_doorbell

    doorbell_button_events = []

    def is_complete_event_sequence(events):
        """Is it a list of matching downs and ups?"""
        return (
            len(events) % 2 == 0
            and all([e[0] == "down" for e in events[0::2]])
            and all([e[0] == "up" for e in events[1::2]])
        )

    def event_durations(events):
        return [
            event_pair[1][1] - event_pair[0][1]
            for event_pair in zip(doorbell_button_events[0::2], doorbell_button_events[1::2])
        ]

    def events_str(events):
        first = events[0][1].timestamp()
        return " ".join([f"{e[0][0]}{e[1].timestamp() - first:.3f}" for e in events])

    def presses(durations):
        return "".join([d <= SHORT_PRESS_MAX and "." or d >= LONG_PRESS_MIN and "-" or "X" for d in durations])

    def events_match_presses_secret(events, secret):
        if not is_complete_event_sequence(events):
            return False
        durations = event_durations(doorbell_button_events)
        return len(durations) == len(secret) and presses(durations) == secret

    while threads_should_run:
        try:
            item = doorbell_button_event_queue.get(block=False)
            doorbell_button_events.append(item)
            doorbell_button_event_queue.task_done()
        except Empty:
            pass

        now = datetime.datetime.now()

        if len(doorbell_button_events) > 0:
            if is_complete_event_sequence(doorbell_button_events) and (
                (
                    len(doorbell_button_events) == 2
                    and now - doorbell_button_events[-1][1] > SINGLE_PRESS_VALID_SEQUENCE_PROCESSING_DELAY
                )
                or (
                    len(doorbell_button_events) > 2
                    and now - doorbell_button_events[-1][1] > MULTIPLE_PRESS_VALID_SEQUENCE_PROCESSING_DELAY
                )
            ):
                log(f"processing events: {events_str(doorbell_button_events)}")
                if events_match_presses_secret(doorbell_button_events, DOOR_OPEN_BUTTON_PRESSES_SECRET):
                    if access_allowed("office", now):
                        should_open_door = True
                elif events_match_presses_secret(doorbell_button_events, DOOR_OPEN_BUTTON_PRESSES_TOP_SECRET_OVERRIDE):
                    should_open_door = True
                else:
                    if len(doorbell_button_events) <= 2 * DOOR_OPEN_BUTTON_DOORBELL_SOUND_MAX_PRESSES:
                        should_ring_doorbell = True

                doorbell_button_events = []

            if (
                not is_complete_event_sequence(doorbell_button_events)
                and now - doorbell_button_events[-1][1] > INVALID_PRESS_SEQUENCE_CLEAR_TIMEOUT
            ):
                log(f"resetting invalid events: {events_str(doorbell_button_events)}")
                # Either spurious GPIO event or too much switch bounce. Do nothing, the person can try again or knock.
                doorbell_button_events = []

        sleep(BUSY_WAIT_SLEEP_DURATION)


# The servo PWM signal buffer in our hardware inverts the signal, this is taken into account
def door_servo_loop():
    global threads_should_run
    global should_open_door

    pwm = GPIO.PWM(SERVO_PWM_OUTPUT_GPIO, SERVO_PULSE_FREQUENCY)
    pwm.start(100)  # 100% high, inverts to 100% low

    def set_servo_pulse_length(pulse_length_us):
        pwm_percentage = 100 * (1 - (pulse_length_us / 1000000) * SERVO_PULSE_FREQUENCY)
        pwm.ChangeDutyCycle(pwm_percentage)

    while threads_should_run:
        if should_open_door:
            should_open_door = False

            GPIO.output(SERVO_POWER_ENABLE_OUTPUT_GPIO, GPIO.HIGH)
            set_servo_pulse_length(DOOR_HANDLE_DOWN_SERVO_PULSE_LENGTH)
            log("handle down")
            sleep(DOOR_HANDLE_MOVEMENT_DURATION)  # handle moving down
            set_servo_pulse_length(DOOR_HANDLE_UP_SERVO_PULSE_LENGTH)
            # log("handle up")
            sleep(DOOR_HANDLE_MOVEMENT_DURATION)  # handle moving up
            set_servo_pulse_length(0)
            GPIO.output(SERVO_POWER_ENABLE_OUTPUT_GPIO, GPIO.LOW)

        sleep(BUSY_WAIT_SLEEP_DURATION)


def doorbell_ring_loop():
    global threads_should_run
    global should_ring_doorbell

    while threads_should_run:
        if should_ring_doorbell:
            should_ring_doorbell = False
            GPIO.output(ERT_CONTACTS_OUTPUT_GPIO, GPIO.HIGH)
            log("ert contacts connected")
            sleep(ERT_CONTACTS_CONNECT_DURATION)
            GPIO.output(ERT_CONTACTS_OUTPUT_GPIO, GPIO.LOW)
            # log("ert contacts disconnected")

        sleep(BUSY_WAIT_SLEEP_DURATION)


def buzzer_loop():
    global threads_should_run
    global buzzer_queue

    while threads_should_run:
        try:
            chirp = buzzer_queue.get(block=False)

            # Play the chirp - an iterable containing tuples (frequency [Hz], duration [s])
            pwm = GPIO.PWM(BUZZER_OUTPUT_GPIO, chirp[0][0])
            pwm.start(0)
            pwm.ChangeDutyCycle(50)
            for freq, duration in chirp:
                pwm.ChangeFrequency(freq)
                sleep(duration)
            pwm.stop(0)
            GPIO.output(BUZZER_OUTPUT_GPIO, GPIO.LOW)

            buzzer_queue.task_done()
        except Empty:
            pass

        sleep(BUSY_WAIT_SLEEP_DURATION)


def heartbeat_loop():
    global threads_should_run

    while threads_should_run:
        GPIO.output(HEARTBEAT_OUTPUT_GPIO, GPIO.HIGH)
        sleep(0.005)
        GPIO.output(HEARTBEAT_OUTPUT_GPIO, GPIO.LOW)
        sleep(1.495)


def access_allowed(kind, time):
    def dot(a, b):
        return a[0] * b[0] + a[1] * b[1]

    weekdays, start_time, end_time = DOOR_OPENING_TIME_RANGE[kind]

    assert weekdays in ("mon-fri", "sat-sun")  # TODO more flexibility
    # Monday is 0 and Sunday is 6
    if not ((weekdays == "mon-fri" and time.weekday() <= 4) or (weekdays == "sat-sun" and time.weekday() >= 5)):
        return False

    min_elapsed_minutes = dot([int(x) for x in start_time.split(":")], (60, 1))
    max_elapsed_minutes = dot([int(x) for x in end_time.split(":")], (60, 1))
    elapsed_day_minutes = time.hour * 60 + time.minute

    return min_elapsed_minutes <= elapsed_day_minutes <= max_elapsed_minutes


def led_on_handler(channel):
    global threads_should_run
    global buzzer_queue
    global should_press_key_button
    global last_led_on_time
    global blink_count

    if not threads_should_run:
        return

    now = datetime.datetime.now()

    sleep(GPIO_EVENT_DOUBLECHECK_DELAY)
    if GPIO.input(INDICATOR_LED_INPUT_GPIO) != GPIO.HIGH:
        return  # spurious input spike

    if last_led_on_time is None or (now - last_led_on_time).seconds >= MIN_BLINK_PAUSE:
        last_led_on_time = now
        blink_count = 1
        log(f"new blinking sequence @ {now}")
    else:
        last_led_on_time = now
        blink_count += 1
        # log(f"blink #{blink_count}")

    if blink_count == KEY_BUTTON_PRESS_AT_BLINK_COUNT and access_allowed("downstairs_immediate", now):
        should_press_key_button = True
    if blink_count == KEY_BUTTON_PRESS_AT_BLINK_COUNT_DELAYED and access_allowed("downstairs_delayed", now):
        should_press_key_button = True


def doorbell_button_handler(channel):
    global doorbell_button_event_queue
    global threads_should_run

    if not threads_should_run:
        return

    now = datetime.datetime.now()

    if GPIO.input(DOORBELL_BUTTON_INPUT_GPIO) == GPIO.HIGH:
        sleep(GPIO_EVENT_DOUBLECHECK_DELAY)
        if GPIO.input(DOORBELL_BUTTON_INPUT_GPIO) == GPIO.HIGH:
            doorbell_button_event_queue.put(("down", now))

    if GPIO.input(DOORBELL_BUTTON_INPUT_GPIO) == GPIO.LOW:
        sleep(GPIO_EVENT_DOUBLECHECK_DELAY)
        if GPIO.input(DOORBELL_BUTTON_INPUT_GPIO) == GPIO.LOW:
            doorbell_button_event_queue.put(("up", now))


def signal_handler(sig, frame):
    global doorbell_button_event_queue
    global buzzer_queue
    global threads_should_run
    global threads

    print("Cleaning up...")

    threads_should_run = False
    for thread in threads:
        thread.join()

    doorbell_button_event_queue.join()
    buzzer_queue.join()

    GPIO.cleanup()
    sys.exit(0)


if __name__ == "__main__":
    global doorbell_button_event_queue
    global buzzer_queue
    global threads
    global threads_should_run

    global last_led_on_time
    global blink_count
    global should_press_key_button

    global should_open_door
    global should_ring_doorbell

    # Communication doorbell_button_handler => doorbell_button_press_processor_loop
    # stores tuples (press start time, press end time)
    doorbell_button_event_queue = Queue()

    # chirps to be played
    buzzer_queue = Queue()

    threads = []
    threads_should_run = True

    last_led_on_time = None
    blink_count = 0
    should_press_key_button = False

    should_open_door = False
    should_ring_doorbell = False

    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)

    GPIO.setup(INDICATOR_LED_INPUT_GPIO, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(DOORBELL_BUTTON_INPUT_GPIO, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

    GPIO.setup(KEY_BUTTON_OUTPUT_GPIO, GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(SERVO_POWER_ENABLE_OUTPUT_GPIO, GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(SERVO_PWM_OUTPUT_GPIO, GPIO.OUT, initial=GPIO.HIGH)
    GPIO.setup(ERT_CONTACTS_OUTPUT_GPIO, GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(HEARTBEAT_OUTPUT_GPIO, GPIO.OUT, initial=GPIO.LOW)
    GPIO.setup(BUZZER_OUTPUT_GPIO, GPIO.OUT, initial=GPIO.LOW)

    for loop in (
        key_button_loop,
        doorbell_button_press_processor_loop,
        door_servo_loop,
        doorbell_ring_loop,
        buzzer_loop,
        heartbeat_loop,
    ):
        threads.append(Thread(target=loop))

    for thread in threads:
        thread.start()

    GPIO.add_event_detect(INDICATOR_LED_INPUT_GPIO, GPIO.RISING, callback=led_on_handler)

    GPIO.add_event_detect(DOORBELL_BUTTON_INPUT_GPIO, GPIO.BOTH, callback=doorbell_button_handler, bouncetime=50)

    signal.signal(signal.SIGINT, signal_handler)
    signal.pause()
