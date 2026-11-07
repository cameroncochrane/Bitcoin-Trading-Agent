# This script is constantly running. It checks the time every 60 seconds. When the time passes onto a new hour, 'main.py' is triggered.

import logging
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path

from src.logger import setup_logging
from src.notification import chat_id, send_email, send_telegram_message
from src.weekly_report import build_weekly_email

logger = logging.getLogger(__name__)

CHECK_INTERVAL_SECONDS = 60
STOP_GRACE_SECONDS = 10 # How long a stuck run gets to exit after a polite stop (SIGTERM) before it's killed (SIGKILL)


def stop_stuck_run(run: subprocess.Popen | None, started: str) -> None:
	"""Stop the previous hourly run if it's still going when the next hour starts, so runs can never pile up.

	A run normally finishes in a few minutes, so one still running after an hour is stuck (e.g. a request that never returns).
	It's asked to stop (SIGTERM), killed (SIGKILL) if it hasn't exited within STOP_GRACE_SECONDS, then logged and reported on
	Telegram. Each trade is saved all-or-nothing, so stopping a run never leaves a half-written trade; that hour's run is lost.

	Example:
		>>> stop_stuck_run(previous_run, "2026-11-07 10:00")  # does nothing if previous_run is None or has finished
	"""
	if run is None or run.poll() is not None: # No previous run, or it finished (poll() also reaps the finished process)
		return

	logger.error("The hourly run started at %s is still running after an hour (pid %d); stopping it.", started, run.pid)
	run.terminate()
	try:
		run.wait(timeout=STOP_GRACE_SECONDS)
	except subprocess.TimeoutExpired:
		logger.error("The run started at %s didn't stop within %ds; killing it.", started, STOP_GRACE_SECONDS)
		run.kill()
		run.wait()

	try:
		send_telegram_message(chat_id, f"BTC agent: the hourly run started at {started} was still running an hour later, so it was stopped. Any trades it made before that are saved. Check the logs for where it got stuck.")
	except Exception: # A failed alert must not stop the hourly loop
		logger.exception("Couldn't send the Telegram alert about the stuck run.")


def main():
	script_dir = Path(__file__).resolve().parent
	logger.info("Scheduler started; main.py will run at the start of every hour.")
	last_hour = datetime.now().replace(minute=0, second=0, microsecond=0)
	run: subprocess.Popen | None = None # The latest hourly run, so a stuck one can be stopped before the next starts
	run_started = ""

	while True:
		time.sleep(CHECK_INTERVAL_SECONDS)  # Check the time every 60 seconds.
		current_hour = datetime.now().replace(minute=0, second=0, microsecond=0)
		if current_hour != last_hour: # When a new hour is reached 'main.py' is run.
			last_hour = current_hour
			try:
				stop_stuck_run(run, run_started) # At most one run at a time, whatever made the last one hang
			except Exception: # Never let the watchdog stop the scheduler
				logger.exception("Couldn't check or stop the previous hourly run.")
			if current_hour.weekday() == 0 and current_hour.hour == 9:
				try:
					send_email(subject="Weekly BTC Portfolio Update", message=build_weekly_email()) # Send portfolio update via email @ 9:00am every Monday
				except Exception: # A failed email must not stop the hourly loop
					logger.exception("Weekly email failed.")
			try:
				run_started = current_hour.strftime("%Y-%m-%d %H:00")
				run = subprocess.Popen(
					[sys.executable, str(script_dir / "main.py")], # Invoke main.py here (it logs its own errors)
					cwd=script_dir,
				)
				logger.info("Started hourly run for %s (pid %d).", run_started, run.pid)
			except Exception:
				logger.exception("Couldn't start main.py.")



if __name__ == "__main__":
	setup_logging()
	main()
	# Invoke app.py (frontend) here
