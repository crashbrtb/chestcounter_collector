
import pyautogui
import time
from screeninfo import get_monitors
from python_imagesearch.imagesearch import imagesearch_region_numLoop

window_title = "Total Battle"
laucher_title = "MainWindow"
path_total_battle = "C:\\Users\\clesi\\AppData\\Roaming\\Scorewarrior\\TotalBattle\\Launcher.exe"
coords_play_button = (934,789)
coord_clan_button = (1013,960)
coord_gift_button = (574,416)

def open_launcher_total_battle():
        print("Total Battle Launcher Starting...")
        # Command to start the application
        pyautogui.hotkey('win', 'r')  # Opens the Run dialog
        pyautogui.write(path_total_battle)
        pyautogui.press('enter')
        # Wait a few seconds for the application to load
        time.sleep(15)
        print("Laucher Total Battle is running!")
        return True
def check_open_total_battle():
    """Checks if the Total Battle application is running."""

    # Find all windows with the given title
    windows = pyautogui.getWindowsWithTitle(window_title)

    # If there's at least one window, activate it
    if windows:
        windows[0].maximize()
        windows[0].activate()
        print("Total Battle application is running!.")
        return True
    else:
        print("Total Battle application isn´t running.")
        return False
def check_open_launcher_total_battle():
    """Checks if the Total Battle application is running."""

    # Find all windows with the given title
    windows = pyautogui.getWindowsWithTitle(laucher_title)

    # If there's at least one window, activate it
    if windows:
        windows[0].maximize()
        windows[0].activate()
        print("Total Battle Laucher application is running!.")
        return True
    else:
        print("Total Battle Laucher application isn´t running.")
        return False
def get_monitor_resolution():
    monitors = get_monitors()
    resolutions = [(m.width, m.height) for m in monitors]
    res = (0,0,resolutions[0][0],resolutions[0][1])
    return res
def close_store(screen_area): #verify if store screen was open
    result = find_image_on_screen("images\\bonussale.png", screen_area)
    if result is None or len(result) == 0:
        print("Store screen wasnt open")
        return False
    else:
        posx = find_image_on_screen("images\\x.png", screen_area)  # if store was open, close it
        if posx is None or len(posx) == 0:
            print("Store x button not found")
            return False
        else:
            click(screen_area[0] + posx[0] + 15, screen_area[1] + posx[1] + 15)
            print("Store closed")
            return True
def find_image_on_screen(path_image, area):
    # area of the screen to be captured initial xy and final xy
    ratio = 0.6
    time_wait = 0.0
    max_att = 5  # maximum number of attempts
    count = 0
    while True:
        pos = imagesearch_region_numLoop(path_image, time_wait, max_att, area[0], area[1], area[2], area[3], ratio)
        # print('Searching for watchtower menu')
        if pos[0] != -1:
            return pos
        count = count + 1
        if count > max_att:
            break
def click(x, y):
    pyautogui.click(x, y)
    time.sleep(2.0)
def open_gift_menu():
    click(coord_clan_button[0], coord_clan_button[1])
    click(coord_gift_button[0], coord_gift_button[1])
    print("Gift menu open")

if __name__ == "__main__":
    print("Cheking if Total Battle already running")
    if check_open_total_battle():
        print("Cheking if store is open")
        if close_store(get_monitor_resolution()):
            print("Store Closed, opening gift menu!")
            open_gift_menu()  #
        else:
            print("Total Battle is ready!!")
            open_gift_menu()  #
    elif open_launcher_total_battle():
        if check_open_launcher_total_battle():
            print("Starting Total Battle")
            click(coords_play_button[0],coords_play_button[1])
            print("Click play button")
            time.sleep(10.0)
            if check_open_total_battle():
                if close_store(get_monitor_resolution()):
                    print("Store Closed, opening gift menu!!")
                    open_gift_menu()  #
    else:
        print("Error02! Cannot open Total Battle")

