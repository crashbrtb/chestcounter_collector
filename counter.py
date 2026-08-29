import cv2
import numpy
import pytesseract
from PIL import ImageGrab
import pyautogui
import mysql.connector
from python_imagesearch.imagesearch import imagesearch_region_numLoop
import time
import configparser
import sys
import datetime
from screeninfo import get_monitors
from python_imagesearch.imagesearch import imagesearch_region_numLoop

# Set default encoding to UTF-8
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')

#---Open TB functions ----------------------------------------------------------------------------------
def open_launcher_total_battle():
        print("Total Battle Launcher Starting...")
        # Command to start the application
        pyautogui.hotkey('win', 'r')  # Opens the Run dialog
        pyautogui.write(coords.get('path_total_battle'))
        pyautogui.press('enter')
        # Wait a few seconds for the application to load
        time.sleep(15)
        print("Laucher Total Battle is running!")
        return True
def check_open_total_battle(window_title):
    """Checks if the Total Battle application is running."""

    # Find all windows with the given title
    windows = pyautogui.getWindowsWithTitle(window_title)

    # If there's at least one window, activate it
    if windows:
        windows[0].maximize()
        windows[0].activate()
        print("Total Battle opened with window title: " + window_title)
        return True
    else:
        print("Total Battle not opened with window title: " + window_title)
        return False
def check_open_launcher_total_battle(laucher_title):
    """Checks if the Total Battle application is running."""

    # Find all windows with the given title
    windows = pyautogui.getWindowsWithTitle(laucher_title)

    # If there's at least one window, activate it
    if windows:
        windows[0].maximize()
        windows[0].activate()
        print("Total Battle Laucher opened with window title: " + laucher_title)
        return True
    else:
        print("Total Battle Laucher not opened with window title: " + laucher_title)
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
def click(x, y):
    pyautogui.click(x, y)
    time.sleep(2.0)       
    
def open_game(window_title):
    print("Cheking if Total Battle already running")
    if check_open_total_battle(window_title):
        print("Cheking if store is open")
        if close_store(get_monitor_resolution()):
            print("Store Closed, game ready!")
            return True  #
        else:
            print("Total Battle is ready!!")
            return True #
    elif open_launcher_total_battle():
        if check_open_launcher_total_battle(laucher_title):
            print("Starting Total Battle")
            click(coords_play_button[0],coords_play_button[1])
            print("Click play button")
            time.sleep(10.0)
            if check_open_total_battle(window_title):
                if close_store(get_monitor_resolution()):
                    print("Store Closed, game ready!!")
                    return True #
    else:
        print("Error02! Cannot open Total Battle")
        return False
    
def close_game(window_title):
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
#---End Open TB functions ----------------------------------------------------------------------------------

# Images capture, treatment and OCR functions ------------------------------------------------------------------------------------------------------
def get_screenshot(area):
    image = ImageGrab.grab(bbox=(area[0], area[1], area[2], area[3]),include_layered_windows=True, xdisplay=None)
    image.save('capture.png')
    image = numpy.array(image)
    return image

def get_grayscale(img):
    return cv2.cvtColor( img, cv2.COLOR_RGB2GRAY)

def remove_noise(img):
    return cv2.medianBlur(img, 5)

def thresholding(img):
    return cv2.threshold( img, 0, 255, cv2.THRESH_BINARY + cv2.THRESH_OTSU)[1]

def upscale_image(img, scale_percent=200):
    width = int(img.shape[1] * scale_percent / 100)
    height = int(img.shape[0] * scale_percent / 100)
    dim = (width, height)
    return cv2.resize(img, dim, interpolation = cv2.INTER_LINEAR)

def ocr_core(img, lang='eng', psm=12, oem=1, whitelist=None):
    config = f'--psm {psm} --oem {oem}'
    if whitelist:
        config += f' -c tessedit_char_whitelist={whitelist}'
    text = pytesseract.image_to_string(img, lang=lang, config=config)
    text = text.replace("——", "")
    return text

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
def find_splitter(text):
    splitter = False
    # check all since the OCR sometimes gets it wrong
    if text.find(":") > -1 or text.find(",") > -1 or text.find(".") > -1:
        if text.find(":") > -1:
            splitter = ":"
        elif text.find(",") > -1:
            splitter = ","
        elif text.find(".") > -1:
            splitter = "."
    if splitter == "":  # none of the above chars were found in the string
        splitter = False
    return splitter

#---End Images capture, treatment and OCR functions ------------------------------------------------------------------------------------------------------

# databases and chest collection functions ----------------------------------------------------------------------------------
def connect_db(account):
    
    try:
        connection = mysql.connector.connect(
            host=account['host'],
            user=account['user'],
            password=account['password'],
            database=account['database'],
            charset="utf8mb4",
            collation="utf8mb4_general_ci",
            use_unicode=True
        )
        
        # Ensure session settings for compatibility across server versions
        cursor = connection.cursor()
        cursor.execute("SET NAMES utf8mb4 COLLATE utf8mb4_general_ci")
        cursor.close()

        print(f"Successful connection to the database '{account['database']}' ({account['account']})")
        return connection
    except mysql.connector.Error as e:  # <-- Change here!
        print(f"Error connecting to MySQL: {e}")
        return None
def insert_chest(connection, name, player, source):
    cursor = connection.cursor()
    try:
        # Check for player name mapping
        query_map = "SELECT correct_name FROM player_name_mappings WHERE ocr_text = %s"
        cursor.execute(query_map, (player,))
        result = cursor.fetchone()
        if result:
            print(f"Player name '{player}' mapped to '{result[0]}'")
            player = result[0]

        # Insert into collected chests
        query = "INSERT INTO collected_chests (name, player, source) VALUES (%s, %s, %s)"
        values = (name, player, source)
        cursor.execute(query, values)
        connection.commit()
        print("Data successfully inserted into collected_chests table")
        return True
    except mysql.connector.Error as e:
        print(f"Error inserting data: {e}")
        return None
    finally:
        cursor.close()

def insert_chest_error(connection, error_value):
    cursor = connection.cursor()
    query = "INSERT INTO errors (error_value) VALUES (%s)"
    values = (error_value)
    try:
        cursor.execute(query, values)
        connection.commit()
        print("Data successfully inserted into errors table")
        return True
    except mysql.connector.Error as e:
        print(f"Error inserting data: {e}")
        return None
    finally:
        cursor.close()
        
def insert_chest_incomplete(connection, name, player, source):
    cursor = connection.cursor()
    query = "INSERT INTO incomplete_chests (name, player, source) VALUES (%s, %s, %s)"
    values = (name, player, source)
    try:
        cursor.execute(query, values)
        connection.commit()
        print("Data successfully inserted into incomplete_chests table")
        return True
    except mysql.connector.Error as e:
        print(f"Error inserting data: {e}")
        return False
    finally:
        cursor.close()
        
def chest_collect(area, connection):
    fp = chest_capture(area)
    try:
        chest = player = source = ""

        if len(fp) < 1:
            print(f"OCR error: No information detected. Content: {fp}")
            if connection:
                insert_chest_error(connection, "OCR_NO_DATA_DETECTED")
                return 2
            else:
                return 0
            
        chest = fp[0] # At least the chest name must exist

        if len(fp) > 1:
            splitter_player = find_splitter(fp[1])
            if splitter_player:
                split_player = fp[1].split(splitter_player, 1)
                if len(split_player) > 1:
                    player = split_player[1].strip()
        else:
            # fp[1] does not exist, player will remain ""
            print("Player information missing or not detected separately.")

        if len(fp) > 2:
            splitter_source = find_splitter(fp[2])
            if splitter_source:
                split_source = fp[2].split(splitter_source, 1)
                if len(split_source) > 1:
                    source = split_source[1].strip()
        else:
            # fp[2] does not exist, source will remain ""
            print("Source information missing or not detected separately.")


        if len(chest) > 0 and len(player) > 0 and len(source) > 0:
            if insert_chest(connection, chest, player, source):
                print(f"Successfully inserted chest: Chest='{chest}', Player='{player}', Source='{source}'. Original lines: {fp}")
                return 1 # success
            else:
                return 0 # DB error
                print(f"Error trying to insert chest: Chest='{chest}', Player='{player}', Source='{source}'. Original lines: {fp}")


        else:
            # Incomplete data even after parsing attempt
            print(f"Incomplete data for insertion: Chest='{chest}', Player='{player}', Source='{source}'. Original lines: {fp}")
            if connection:
                
                # Build a more detailed error string with what was captured
                error_details = []
                if fp: error_details.append(f"Line1: {fp[0] if len(fp) > 0 else 'N/A'}")
                if len(fp) > 1: error_details.append(f"Line2: {fp[1]}")
                if len(fp) > 2: error_details.append(f"Line3: {fp[2]}")
                # Add more lines if fp can be longer
                error_value = f"INCOMPLETE_PARSE_DATA: {' | '.join(error_details)}. Detected: C='{chest}', P='{player}', S='{source}'"
                #insert_chest_error(connection, error_value)
                if(insert_chest_incomplete(connection, chest, player, source)):
                    return 2
                else:
                    return 0
            else:
                return 0

    except Exception as e:
        print(f"An error occurred: {e}")
        print(fp)
        return False
    
def chest_capture(area):
    image = get_screenshot(area)
    image = get_grayscale(image)
    image = upscale_image(image, 200)
    image = thresholding(image)
    text = ocr_core(image, psm=6)
    raw_rows = text.split("\n")
    rows = list()
    # remove empty rows
    for row in raw_rows:
        if len(row.strip()) == 0:
            continue
        rows.append(row)
    return rows
    
def check_text_in_area(area, text_to_find):
    """
    Checks if a specific text is present in a given screen area using OCR.
    Returns True if found, False otherwise.
    """
    image = get_screenshot(area)
    image = get_grayscale(image)
    image = upscale_image(image, 200)
    image = thresholding(image)
    detected_text = ocr_core(image, psm=6)
    
    # Clean up text for comparison
    clean_detected = detected_text.strip().lower()
    clean_target = text_to_find.strip().lower()
    
    print(f"Checking area {area}. Found: '{clean_detected}'. Target: '{clean_target}'")
    
    if clean_target in clean_detected:
        return True
    return False

def start_chest_collection(cord_menu_button_open_chest, chest_area, account):
    counter = 0 #counter for chests collected
    counter_errors = 0 #Counter for errors
    
    print(f"Starting DB connection for account: {account['account']}...")
    connection = connect_db(account)
    
    if not connection:
        print("Failed to establish database connection. Aborting collection.")
        return

    try:
        while True:
            pos = find_image_on_screen("images\\open.png", cord_menu_button_open_chest)  # Search for go button on citadels list
            if pos is None or len(pos) == 0:
                print("There arent more chests")
                break
            else:
                result = chest_collect(chest_area, connection)
                if (result == 1):
                    pyautogui.click(cord_menu_button_open_chest[0] + pos[0] + 80, cord_menu_button_open_chest[1] + pos[1] + 20)
                    time.sleep(0.5)
                    counter = counter + 1
                elif (result == 2):
                    pyautogui.click(cord_menu_button_open_chest[0] + pos[0] + 80, cord_menu_button_open_chest[1] + pos[1] + 20)
                    time.sleep(0.5)
                    counter_errors = counter_errors + 1
                elif (result == 0):
                    counter_errors = counter_errors + 1
                    break
                    
        print(counter_errors, " chests incorrects")
        print(counter, " chests collecteds")
        print("account: ", account['account'])
                
    finally:
        if connection and connection.is_connected():
            connection.close()
            print("Database connection closed.")
def open_gift_menu(coord_clan_button, coord_gift_button):
    click(coord_clan_button[0], coord_clan_button[1])
    click(coord_gift_button[0], coord_gift_button[1])
    print("Gift menu open")
    
#---Chest capture functions ----------------------------------------------------------------------------------
#--Account functions ----------------------------------------------------------------------------------
def select_account_on_screen(area, account_name, account_menu_button, account_name_display_area, switch_progress_confirm_button_area, screen_area):
    # First, check if the account is already selected
    print(f"Checking if account '{account_name}' is already active...")
    if check_text_in_area(account_name_display_area, account_name):
        print(f"Account '{account_name}' is already active. Skipping selection.")
        return True

    # Try to find the account twice: once initially, and once after scrolling if needed
    for attempt in range(2):
        print(f"Attempt #{attempt + 1} to find account '{account_name}'")
        
        # Capture and process image
        pyautogui.click(account_menu_button[0], account_menu_button[1])
        time.sleep(1.0) # Wait for menu to open/refresh
        
        image = get_screenshot(area)
        image_gray = get_grayscale(image)
        # We use consistent scaling factor
        scale_factor = 2
        image_upscaled = upscale_image(image_gray, scale_percent=200)
        image_thresh = thresholding(image_upscaled)
        
        # Get detailed data from tesseract including coordinates
        # Output format is a dict with lists for 'left', 'top', 'width', 'height', 'text', etc.
        data = pytesseract.image_to_data(image_thresh, output_type=pytesseract.Output.DICT, config='--psm 6')
        
        n_boxes = len(data['text'])
        
        # Iterate through found text to find the account name
        for i in range(n_boxes):
            text = data['text'][i].strip()
            if not text:
                continue
                
            # Check if the detected text is part of the account name
            # We handle case-insensitive comparison
            if text.lower() in account_name.lower() and len(text) > 2:
                (x, y, w, h) = (data['left'][i], data['top'][i], data['width'][i], data['height'][i])
                
                # Adjust coordinates back to original screen scale
                # Since we upscaled by 200% (factor 2)
                center_x_rel = (x + w / 2) / scale_factor
                center_y_rel = (y + h / 2) / scale_factor
                
                # Calculate absolute screen coordinates
                abs_x = area[0] + int(center_x_rel)
                abs_y = area[1] + int(center_y_rel)
                
                print(f"Account '{account_name}' found at relative ({center_x_rel}, {center_y_rel}). Clicking at ({abs_x}, {abs_y})")
                pyautogui.click(abs_x, abs_y)
                time.sleep(1.0)
                if check_progress_confirm_button(switch_progress_confirm_button_area):
                    time.sleep(15.0)
                    if check_open_total_battle(window_title):
                        if close_store(get_monitor_resolution()):
                            print("Store Closed, game ready!!")
                        return True
                else:
                    print("Button confirm not found")

        print(f"Account '{account_name}' not found in current view.")
        
        # If not found and this was the first attempt, scroll down and try again
        if attempt == 0:
            print("Scrolling down to search again...")
            # Calculate center of the search area to ensure scroll happens over the list
            center_area_x = area[0] + (area[2] - area[0]) // 2
            center_area_y = area[1] + (area[3] - area[1]) // 2
            
            pyautogui.moveTo(center_area_x, center_area_y)
            for _ in range(5):
                pyautogui.scroll(-100) # Scroll down (negative value)
                time.sleep(0.1) # Small delay between scrolls
            time.sleep(1.0) # Wait for scroll animation to settle
            
    print(f"Account '{account_name}' not found after scrolling.")
    return False

def load_accounts_from_config():
    accounts_data = []
    for section in config.sections():
        if section.startswith('account'):
            account_info = {
                'section': section,
                'account': config.get(section, 'account', fallback='N/A'),
                'host': config.get(section, 'host', fallback='N/A'),
                'user': config.get(section, 'user', fallback='N/A'),
                'password': config.get(section, 'password', fallback='N/A'),
                'database': config.get(section, 'database', fallback='N/A')
            }
            accounts_data.append(account_info)
    return accounts_data

def check_correct_accounts(account_name, account_name_display_area):
    if check_text_in_area(account_name_display_area, account_name):
        return True
    else:
        return False
def check_progress_confirm_button(switch_progress_confirm_button_area):  
    posx = find_image_on_screen("images\\progress_confirm.png", switch_progress_confirm_button_area)
    if posx is None or len(posx) == 0:
        print("Confirm button not found")
        return False
    else:
        # Click in the center of the defined area
        center_x = switch_progress_confirm_button_area[0] + (switch_progress_confirm_button_area[2] - switch_progress_confirm_button_area[0]) // 2
        center_y = switch_progress_confirm_button_area[1] + (switch_progress_confirm_button_area[3] - switch_progress_confirm_button_area[1]) // 2
        click(center_x, center_y)
        print(f"Button confirm clicked at ({center_x}, {center_y})")
        return True

#End Account functions ----------------------------------------------------------------------------------

if __name__ == "__main__":
    print("Starting chest collection..." + datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
    print("--------------------------------")
    # Load configuration using configparser
    config = configparser.ConfigParser()
    config.read('position.cfg')

    # Helper function to parse configuration values
    def parse_config_value(value):
        if value is None:
            return None
        value = value.strip()
        if value.isdigit():
            return int(value)
        elif value.lower() == 'true':
            return True
        elif value.lower() == 'false':
            return False
        elif value.startswith('(') and value.endswith(')'):
            return tuple(map(int, value[1:-1].split(',')))
        return value

    # Read variables from COORDINATES section
    if 'COORDINATES' in config:
        coords = config['COORDINATES']
        coords_play_button = parse_config_value(coords.get('coords_play_button'))
        coord_clan_button = parse_config_value(coords.get('coord_clan_button'))
        coord_gift_button = parse_config_value(coords.get('coord_gift_button'))
        chest_area = parse_config_value(coords.get('chest_area'))
        open_button = parse_config_value(coords.get('open_button'))
        screen_area = parse_config_value(coords.get('screen_area'))
        cord_menu_button_open_chest = parse_config_value(coords.get('cord_menu_button_open_chest'))
        account_menu_button = parse_config_value(coords.get('account_menu_button'))
        accounts_area = parse_config_value(coords.get('accounts_area'))
        account_name_display_area = parse_config_value(coords.get('account_name_display_area'))
        window_title = coords.get('window_title')
        laucher_title = coords.get('laucher_title')
        switch_progress_name_area = parse_config_value(coords.get('switch_progress_name_area'))
        switch_progress_confirm_button_area = parse_config_value(coords.get('switch_progress_confirm_button_area'))
    else:
        print("Section [COORDINATES] not found in position.cfg")
        sys.exit(1)
    
    #---Open TB ----------------------------------------------------------------------------------
    if open_game(window_title): #open game and check if store is closed
        accounts_list = load_accounts_from_config()
        if not accounts_list:
            print("No accounts found in position.cfg")
            sys.exit(1)
        else:
            for idx, acc in enumerate(accounts_list, 1):
                print(f"Account #{idx} [{acc['section']}]:")

                if(select_account_on_screen(accounts_area, acc['account'], account_menu_button, account_name_display_area, switch_progress_confirm_button_area, screen_area)):
                    print("Account selected: " + acc['account'])
                    open_gift_menu(coord_clan_button, coord_gift_button)
                    if(check_correct_accounts(acc['account'], account_name_display_area)):
                        start_chest_collection(cord_menu_button_open_chest, chest_area, acc)
                        print("End of collection for account: " + acc['account'])
                    else:
                        print("Account not correct canceling collection: " + acc['account'])
                        close_game(window_title)
                        sys.exit(1)
                else:
                    print("Account not found: " + acc['account'])
                    close_game(window_title)
                    sys.exit(1)
        print("End of collection for all accounts")
        close_game(window_title)
        print("Game closed")
        print("--------------------------------")
        print("Chest collection finished at: " + datetime.datetime.now().strftime("%Y-%m-%d %H:%M:%S"))
        
    else:
        print("Error01! Game not opened")
        sys.exit(1)   
        
