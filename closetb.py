import pyautogui
import time

window_title = "Total Battle"
def check_open_total_battle():
    """Checks if the Total Battle application is running."""

    # Find all windows with the given title
    windows = pyautogui.getWindowsWithTitle(window_title)

    # If there's at least one window, activate it
    if windows:
        windows[0].close()
        print("Total Battle application was close!.")
        return True
    else:
        print("Total Battle application isn´t running.")
        return False

if __name__ == "__main__":
    check_open_total_battle()