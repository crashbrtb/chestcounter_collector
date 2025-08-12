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
# Set default encoding to UTF-8
sys.stdout.reconfigure(encoding='utf-8')
sys.stderr.reconfigure(encoding='utf-8')


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
# Images capture, treatment and OCR functions ------------------------------------------------------------------------------------------------------

# databases functions ----------------------------------------------------------------------------------
def connect_db():
    config = configparser.ConfigParser()
    config.read('position.cfg')

    try:
        connection = mysql.connector.connect(
            host=config.get('database', 'host'),
            user=config.get('database', 'user'),
            password=config.get('database', 'password'),
            database=config.get('database', 'database'),
            charset="utf8mb4" # Only the 'charset' parameter is required
        )
        print("Successful connection to the database 'chestcounter'")
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
# ------------------------------------------------------------------------------------------------------
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



def chest_colect(area):
    fp = chest_capture(area)
    try:
        chest = player = source = ""

        if len(fp) < 1:
            print(f"OCR error: No information detected. Content: {fp}")
            connection = connect_db()
            if connection:
                insert_chest_error(connection, "OCR_NO_DATA_DETECTED")
                connection.close()
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
            connection = connect_db()
            if connection:
                insert_chest(connection, chest, player, source)
                connection.close()
                return 1 # success
            else:
                return 0 # DB error

        else:
            # Incomplete data even after parsing attempt
            print(f"Incomplete data for insertion: Chest='{chest}', Player='{player}', Source='{source}'. Original lines: {fp}")
            connection = connect_db()
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
                connection.close()
            else:
                return 0

    except Exception as e:
        print(f"An error occurred: {e}")
        print(fp)
        return False

def find_image_on_screen(caminho_imagem, area):
    # area of the screen to be captured initial xy and final xy
    ratio = 0.6
    time_wait = 0.0
    max_att = 3  # maximum number of attempts
    count = 0
    while True:
        pos = imagesearch_region_numLoop(caminho_imagem, time_wait, max_att, area[0], area[1], area[2], area[3],
                                         ratio)
        # print('Searching for watchtower menu')
        if pos[0] != -1:
            return pos
        count = count + 1
        if count > max_att:
            break

if __name__ == "__main__":
    global_vars = {}
    with open('position.cfg', 'r') as f:
        for line in f:
            if line.strip():  # Ignore empty lines
                if line.startswith('['):
                    print("") #just to ignore name of section
                else:
                    var, value = line.split('=')
                    var = var.strip()  # Remove whitespace before and after the variable name
                    value = value.strip()  # Remove whitespace before and after the value

                    # Convert value (int, tuple or str)
                    if value.isdigit():
                        value = int(value)
                    elif value.startswith('(') and value.endswith(')'):
                        value = tuple(map(int, value[1:-1].split(',')))
                    elif value.startswith('str(') and value.endswith(')'):
                        value = str(value[4:-1])

                    # put value to global_vars
                    global_vars[var] = value

    chest_area = global_vars['chest_area']
    open_button = global_vars['open_button']
    screen_area = global_vars['screen_area']
    cord_menu_button_open_chest = global_vars['cord_menu_button_open_chest']
    counter = 0 #Counter for citadels



    counter_errors = 0
    while True:
        pos = find_image_on_screen("images\\open.png", cord_menu_button_open_chest)  # Search for go button on citadels list
        if pos is None or len(pos) == 0:
            print("There arent more chests")
            break
        else:
            if (chest_colect(chest_area)==1):
                pyautogui.click(cord_menu_button_open_chest[0] + pos[0] + 80, cord_menu_button_open_chest[1] + pos[1] + 20)
                time.sleep(0.5)
                counter = counter + 1
            elif (chest_colect(chest_area)==2):
                pyautogui.click(cord_menu_button_open_chest[0] + pos[0] + 80, cord_menu_button_open_chest[1] + pos[1] + 20)
                time.sleep(0.5)
                counter_errors = counter_errors + 1
            elif (chest_colect(chest_area)==0):
                counter_errors = counter_errors + 1
                break
                
    print(counter_errors, " chests incorrects")
    print(counter, " chests collecteds")