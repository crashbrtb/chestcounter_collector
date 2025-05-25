import cv2
import numpy
import pytesseract
from PIL import ImageGrab
import pyautogui
import mariadb
from python_imagesearch.imagesearch import imagesearch_region_numLoop
import time
import configparser


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

def ocr_core(img):
    text = pytesseract.image_to_string(img, lang='eng', config='--psm 12 --oem 1')
    text = text.replace("——", "")
    return text
# Images capture, treatment and OCR functions ------------------------------------------------------------------------------------------------------

# databases functions ----------------------------------------------------------------------------------
def connect_db():
    config = configparser.ConfigParser()
    config.read('position.cfg')

    try:
        connection = mariadb.connect(
            host=config.get('database', 'host'),
            user=config.get('database', 'user'),
            password=config.get('database', 'password'),
            database=config.get('database', 'database')
        )
        print("Successful connection to the database 'chestcounter'")
        return connection
    except mariadb.Error as e:
        print(f"Error connecting to MariaDB: {e}")
        return None

def insert_chest(connection, name, player, source):
    cursor = connection.cursor()
    query = "INSERT INTO collected_chests (name, player, source) VALUES (?, ?, ?)"
    values = (name, player, source)
    try:
        cursor.execute(query, values)
        connection.commit()
        print("Data successfully inserted into collected_chests table")
        return True
    except mariadb.Error as e:
        print(f"Error inserting data: {e}")
        return None
    finally:
        cursor.close()

def insert_chest_error(connection, error_value):
    cursor = connection.cursor()
    query = "INSERT INTO errors (error_value) VALUES (?)"
    values = (error_value)
    try:
        cursor.execute(query, values)
        connection.commit()
        print("Data successfully inserted into errors table")
        return True
    except mariadb.Error as e:
        print(f"Error inserting data: {e}")
        return None
    finally:
        cursor.close()
def chest_capture(area):
    image = get_screenshot(area)
    image = get_grayscale(image)
    image = thresholding(image)
    text = ocr_core(image)
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
    if text.find(":") > -1 or text.find(",") > -1 or text.find(".") > -1 or text.find(",") > -1:
        if text.find(":") > -1:
            splitter = ":"
        elif text.find(",") > -1:
            splitter = ","
        elif text.find(".") > -1:
            splitter = "."
        elif text.find(",") > -1:
            splitter = ","
    if splitter == "":  # none of the above chars were found in the string
        splitter = False
    return splitter



def chest_colect(area):
    fp = chest_capture(area)
    try:
        chest = player = source = ""
        chest = fp[0]

        splitter = find_splitter(fp[1])
        if (splitter != False):
            split_player = fp[1].split(splitter, 1)
            player = split_player[1].strip()

        splitter = find_splitter(fp[2])

        if (splitter != False):
            split_source = fp[2].split(splitter, 1)
            source = split_source[1].strip()

        if len(chest) > 0 and len(player) > 0 and len(source) > 0:
            connection = connect_db()
            if connection:
                insert_chest(connection, chest, player, source)
                connection.close()
                return 1
            else:
                return 0

        else:
            connection = connect_db()
            if connection:
                error_value = fp[0] + "-" + fp[1] + "-" + fp[2]
                insert_chest_error(connection, error_value)
                connection.close()
                return 2
            else:
                return 0

    except Exception as e:
        print(f"Ocorreu um erro: {e}")
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
            if line.strip():  # Ignora linhas vazias
                if line.startswith('['):
                    print("") #just to ignore name of section
                else:
                    var, value = line.split('=')
                    var = var.strip()  # Remove espaços em branco antes e depois do nome da variável
                    value = value.strip()  # Remove espaços em branco antes e depois do valor

                    # Convert value (int, tuple ou str)
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
                counter_errors = counter_errors + 1
    print(counter_errors, " chests incorrects")
    print(counter, " chests collecteds")