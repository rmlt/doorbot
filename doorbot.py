#!/usr/bin/env python3

from datetime import datetime
import signal
import sys
import threading
from threading import Thread
from time import sleep

import RPi.GPIO as GPIO


# GPIO 0-8 have default pull-ups
# GPIO 9-27 have default pull-downs
INDICATOR_LED_INPUT_GPIO = 6
DOORBELL_BUTTON_INPUT_GPIO = 26

KEY_BUTTON_OUTPUT_GPIO = 21
SERVO_PWM_OUTPUT_GPIO = 5
HEARTBEAT_OUTPUT_GPIO = 25

MIN_BLINK_PAUSE = 3  # seconds, a blink after at least that time starts a new blinking (phone blinks every 1.2s)
KEY_BUTTON_PRESS_AT_BLINK_COUNT = 4  # 3.6s
KEY_BUTTON_PRESS_AT_BLINK_COUNT_DELAYED = 35  # up to 37

KEY_BUTTON_PRESS_DURATION = 5  # seconds, should be < MIN_BLINK_PAUSE + (KEY_BUTTON_PRESS_AT_BLINK_COUNT - 1) * 1.2

DOOR_HANDLE_DOWN_DURATION = 2
SERVO_PULSE_FREQUENCY = 50
DOOR_HANDLE_DOWN_SERVO_PULSE_LENGTH = 2200  # us
DOOR_HANDLE_UP_SERVO_PULSE_LENGTH = 800  # us

DEBOUNCE_DURATION = 0.05

# TODO Fix pi time zone
HOUR_MIN = 7  # 8:00
HOUR_MAX = 18  # 19:00


def format_time(time):
    return time.strftime("%d-%m-%Y %H:%M:%S.%f")


def key_button_loop():
    global threads_should_run
    global should_press_key_button

    while threads_should_run:
        if should_press_key_button:
            should_press_key_button = False
            GPIO.output(KEY_BUTTON_OUTPUT_GPIO, GPIO.HIGH)
            print("button down")
            sleep(KEY_BUTTON_PRESS_DURATION)
            GPIO.output(KEY_BUTTON_OUTPUT_GPIO, GPIO.LOW)
            print("button up")

        sleep(0.1)


# The servo buffer in our hardware inverts the signal, this is taken into account
def door_servo_loop():
    global threads_should_run
    global should_open_door

    pwm = GPIO.PWM(SERVO_PWM_OUTPUT_GPIO, SERVO_PULSE_FREQUENCY)
    pwm.start(100)  # 100% high, inverted to 100% low

    def set_servo_pulse_length(pulse_length_us):
        pwm_percentage = 100 * (1 - (pulse_length_us / 1000000) * SERVO_PULSE_FREQUENCY)
        pwm.ChangeDutyCycle(pwm_percentage)

    while threads_should_run:
        # TODO serwo power on/off
        if should_open_door:
            should_open_door = False
            set_servo_pulse_length(DOOR_HANDLE_DOWN_SERVO_PULSE_LENGTH)
            print("handle down")
            sleep(DOOR_HANDLE_DOWN_DURATION)
            set_servo_pulse_length(DOOR_HANDLE_UP_SERVO_PULSE_LENGTH)
            print("handle up")

        sleep(0.1)


def heartbeat_loop():
    global threads_should_run

    while threads_should_run:
        GPIO.output(HEARTBEAT_OUTPUT_GPIO, GPIO.HIGH)
        sleep(0.005)
        GPIO.output(HEARTBEAT_OUTPUT_GPIO, GPIO.LOW)
        sleep(1.495)


def quick_access_allowed(time):
    # Monday is 0 and Sunday is 6
    return time.weekday() <= 4 and HOUR_MIN <= time.hour <= HOUR_MAX


def delayed_access_allowed(time):
    return time.weekday() >= 5 and HOUR_MIN <= time.hour <= HOUR_MAX


def led_on_handler(channel):
    global should_press_key_button
    global last_led_on_time
    global blink_count

    now = datetime.now()

    sleep(DEBOUNCE_DURATION)
    if GPIO.input(INDICATOR_LED_INPUT_GPIO) != GPIO.LOW:
        return  # spurious input spike

    if last_led_on_time is None or (now - last_led_on_time).seconds >= MIN_BLINK_PAUSE:
        last_led_on_time = now
        blink_count = 1
        # print(f"new blinking sequence @ {format_time(now)}")
    else:
        last_led_on_time = now
        blink_count += 1
        # print(f"blink #{blink_count} @ {format_time(now)}")

    if blink_count == KEY_BUTTON_PRESS_AT_BLINK_COUNT and quick_access_allowed(now):
        should_press_key_button = True

    if blink_count == KEY_BUTTON_PRESS_AT_BLINK_COUNT_DELAYED and delayed_access_allowed(now):
        should_press_key_button = True


def doorbell_button_handler(channel):
    global should_open_door

    if GPIO.input(DOORBELL_BUTTON_INPUT_GPIO) == GPIO.HIGH:
        sleep(DEBOUNCE_DURATION)
        if GPIO.input(DOORBELL_BUTTON_INPUT_GPIO) == GPIO.HIGH:
            should_open_door = True
            sleep(DOOR_HANDLE_DOWN_DURATION + 1)


def signal_handler(sig, frame):
    global threads_should_run
    global threads

    print("Cleaning up...")
    threads_should_run = False
    for thread in threads:
        thread.join()
    GPIO.cleanup()
    sys.exit(0)


if __name__ == "__main__":
    global threads
    global threads_should_run

    global should_press_key_button
    global should_open_door
    global last_led_on_time
    global blink_count

    threads = []
    threads_should_run = True

    should_press_key_button = False
    should_open_door = False
    last_led_on_time = None
    blink_count = 0

    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)

    GPIO.setup(INDICATOR_LED_INPUT_GPIO, GPIO.IN, pull_up_down=GPIO.PUD_UP)
    GPIO.setup(DOORBELL_BUTTON_INPUT_GPIO, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)

    GPIO.setup(KEY_BUTTON_OUTPUT_GPIO, GPIO.OUT)
    GPIO.setup(SERVO_PWM_OUTPUT_GPIO, GPIO.OUT, initial=GPIO.HIGH)
    GPIO.setup(HEARTBEAT_OUTPUT_GPIO, GPIO.OUT)

    threads.append(Thread(target=key_button_loop))
    threads.append(Thread(target=door_servo_loop))
    threads.append(Thread(target=heartbeat_loop))

    for thread in threads:
        thread.start()

    # inverted input, FALLING means LED turned on
    GPIO.add_event_detect(INDICATOR_LED_INPUT_GPIO, GPIO.FALLING, callback=led_on_handler)
    GPIO.add_event_detect(DOORBELL_BUTTON_INPUT_GPIO, GPIO.BOTH, callback=doorbell_button_handler)

    signal.signal(signal.SIGINT, signal_handler)
    signal.pause()
