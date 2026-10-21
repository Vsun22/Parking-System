import os
import sys
import shutil
import socket
import signal
import subprocess
import tempfile
import time
import re
from enum import Enum, auto
from pathlib import Path
from typing import Any, Optional

# colorama to ensure ANSI escape codes work across all terminals
from colorama import Fore, Style
from colorama import init as init_colorama

# WALL OF CONSTANTS

# enable verbose debug logging throughout the application
IS_DEBUG = True

# print every SQL statement right before it is executed
SHOW_SQL = True

# Determine if the current operating system is Windows
#we only support windows and linux
IS_WINDOWS = (os.name == "nt")

# Determines the base application directory.
# If frozen by PyInstaller, use the executable's directory
# Otherwise, use the script's directory.
if getattr(sys, "frozen", False):
    CONST_APP_DIRECTORY = Path(sys.executable).resolve().parent
else:
    CONST_APP_DIRECTORY = Path(__file__).resolve().parent

# Define all critical file and folder paths.
CONST_DATA_DIRECTORY = CONST_APP_DIRECTORY / "app_data"
CONST_DATABASE_DIRECTORY = CONST_DATA_DIRECTORY / "db_data"
CONST_FIRST_LAUNCH_MARKER_FILE = CONST_DATA_DIRECTORY / ".initialized"
CONST_SERVER_PID_FILE = CONST_DATA_DIRECTORY / "mysql.pid"
CONST_SERVER_LOG_FILE = CONST_DATA_DIRECTORY / "server_output.log"
CONST_ROOT_SETUP_SQL_FILE = CONST_DATA_DIRECTORY / "init.sql"

# Database connection credentials and network settings.
DATABASE_HOST = "127.0.0.1"
DATABASE_USER = "root"
DATABASE_PASSWORD = "2122"
DATABASE_NAME = "smart_parking"

# Timeouts and limits for process management.
CONST_STARTUP_TIMEOUT_SECONDS = 60
CONST_SHUTDOWN_TIMEOUT_SECONDS = 30
CONST_UNIX_SOCKET_MAX_PATH_LENGTH = 100

# Parking lot physical constraints.
CONST_TOTAL_PARKING_SLOTS = 200

# Grid display constraints.
CONST_AUTO_COLUMNS_FLAG = 0
CONST_MAX_GRID_COLUMNS = 9

# Extra paths to search for database executables on Linux systems.
CONST_EXTRA_LINUX_PROGRAM_FOLDERS = ["/usr/sbin", "/usr/local/sbin", "/usr/libexec", "/sbin"]

# Marker used to detect when the MySQL client has finished processing a query.
CONST_SQL_END_MARKER_COLUMN = "__sql_finished__"
CONST_SQL_END_MARKER_QUERY = f"SELECT 'end' AS {CONST_SQL_END_MARKER_COLUMN};"

# Lines printed by the MySQL client that we want to silently ignore.
CONST_IGNORED_CLIENT_OUTPUT_LINES = ("[Warning]", "Deprecated program name")

# Token used in ASCII art to represent where the license plate should be injected.
CONST_ASCII_PLATE_TOKEN = "{plate}"

# Mapping of string tokens to actual ANSI color codes for ASCII art rendering.
CONST_ASCII_COLOR_TOKENS = {
    "{red}": Fore.RED,
    "{green}": Fore.GREEN,
    "{yellow}": Fore.YELLOW,
    "{cyan}": Fore.CYAN,
    "{reset}": Style.RESET_ALL,
}

# Characters that must be escaped when formatting raw Python strings into SQL literals.
CONST_SQL_STRING_ESCAPE_SEQUENCE = (
    ("\\", "\\\\"),
    ("'", "\\'"),
    ('"', '\\"'),
    ("\0", "\\0"),
    ("\n", "\\n"),
    ("\r", "\\r"),
    ("\x1a", "\\Z"),
)

# DATABASE SERVER FLAGS DO NOT MODIFY, i have no idea what they do 
# but they reduce the folder size when making a completely new instance of mariadb/mysql
# like really bare bones for our simple demonstration

SLIM_SERVER_FLAGS = [
    "--performance-schema=OFF",
    "--skip-log-bin",
    # mariadb wants buffer pool >= 6MB
    "--innodb-buffer-pool-size=12M",
    # mariadb minimums for the log sizes below
    "--loose-innodb-log-file-size=4M",
    "--loose-innodb-log-buffer-size=2M",
    "--loose-innodb-undo-tablespaces=2",
    "--loose-innodb-max-undo-log-size=10M",
    "--loose-innodb-undo-log-truncate=ON",
    "--max-connections=10",
    "--table-open-cache=10",
    "--table-definition-cache=400",
    "--key-buffer-size=128K",
    "--loose-aria-pagecache-buffer-size=128K",
    "--innodb-flush-log-at-trx-commit=2",
    "--innodb-doublewrite=0",
]
#makes mariadb behave live MYsql
MARIADB_LIKE_MYSQL_FLAGS = [
    "--sql-mode=ONLY_FULL_GROUP_BY,STRICT_TRANS_TABLES,NO_ZERO_IN_DATE,NO_ZERO_DATE,"
    "ERROR_FOR_DIVISION_BY_ZERO,NO_ENGINE_SUBSTITUTION",
    "--character-set-server=utf8mb4",
    "--default-storage-engine=InnoDB",
    "--explicit-defaults-for-timestamp=ON",
]

# RAW SQL COMMANDS (DO NOT MODIFY)

# creating a new user so we dont have to deal with the existing and current one 
#or deal with their passwords ever again
ROOT_SETUP_SQL = f"""
FLUSH PRIVILEGES;
CREATE USER IF NOT EXISTS 'root'@'127.0.0.1' IDENTIFIED BY '{DATABASE_PASSWORD}';
ALTER USER 'root'@'127.0.0.1' IDENTIFIED BY '{DATABASE_PASSWORD}';
GRANT ALL PRIVILEGES ON *.* TO 'root'@'127.0.0.1' WITH GRANT OPTION;
CREATE USER IF NOT EXISTS 'root'@'localhost' IDENTIFIED BY '{DATABASE_PASSWORD}';
ALTER USER 'root'@'localhost' IDENTIFIED BY '{DATABASE_PASSWORD}';
GRANT ALL PRIVILEGES ON *.* TO 'root'@'localhost' WITH GRANT OPTION;
FLUSH PRIVILEGES;
"""
#all the commands are paranoid and so this if not exists 
#becasue i counldnt find a good reason to not use it
# IF EXISTS and IF NOT EXISTS
SCHEMA_SQL = r"""
-- TABLES

-- Price list: one row per vehicle type. 
-- UNIQUE means a type cannot be listed twice.
-- The CHECKs only allow the three known types and a rate above zero.
CREATE TABLE IF NOT EXISTS Vehicle_Categories (
    category_id INT PRIMARY KEY AUTO_INCREMENT,
    vehicle_type VARCHAR(20) NOT NULL UNIQUE CHECK (vehicle_type IN ('2-Wheeler', '4-Wheeler', 'Heavy')),
    hourly_rate DECIMAL(8, 2) NOT NULL CHECK (hourly_rate > 0)
) ;

-- Every plate that has been parked, and its category i.e(foreign key to the price list).
-- The CHECK only allows 4 to 20 letters or digits.
--  Plates are upper-cased by Normalize_Plate before .
CREATE TABLE IF NOT EXISTS Vehicles (
    vehicle_no VARCHAR(20) PRIMARY KEY CHECK (vehicle_no REGEXP '^[A-Z0-9]{4,20}$'),
    category_id INT NOT NULL,
    FOREIGN KEY (category_id) REFERENCES Vehicle_Categories(category_id)
) ;


-- Vehicles inside the lot right now (one row = one open ticket).
-- UNIQUE vehicle_no so a vehicle cannot be parked twice.
-- UNIQUE slot_no so a slot cannot hold two vehicles.
-- entry_time is stamped by the server when the ticket is issued.
CREATE TABLE IF NOT EXISTS Active_Parking (
    ticket_id INT PRIMARY KEY AUTO_INCREMENT,
    vehicle_no VARCHAR(20) NOT NULL UNIQUE,
    slot_no INT NOT NULL UNIQUE CHECK (slot_no BETWEEN 1 AND 3000),
    entry_time DATETIME NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (vehicle_no) REFERENCES Vehicles(vehicle_no)
) ;

-- Finished stays: one row is added at every checkout.
-- The CHECKs stop a negative fee and an exit time before the entry time.
-- The index on exit_time makes looking up stays by exit date fast.
CREATE TABLE IF NOT EXISTS Parking_History (
    history_id INT PRIMARY KEY AUTO_INCREMENT,
    vehicle_no VARCHAR(20) NOT NULL,
    slot_no INT NOT NULL,
    entry_time DATETIME NOT NULL,
    exit_time DATETIME NOT NULL,
    total_fee DECIMAL(10, 2) NOT NULL CHECK (total_fee >= 0),
    CHECK (exit_time >= entry_time),
    FOREIGN KEY (vehicle_no) REFERENCES Vehicles(vehicle_no),
    INDEX idx_history_exit_time (exit_time)
) ;

-- Money collected per day 
-- the triggers on Parking_History keep it up to date
CREATE TABLE IF NOT EXISTS Revenue_Audit (
    audit_date DATE PRIMARY KEY,
    total_collection DECIMAL(12, 2) NOT NULL DEFAULT 0.00
) ;

-- ON DUPLICATE KEY UPDATE with vehicle_type = vehicle_type changes nothing,
--  it only stops the duplicate error so this is safe to run on every start.
INSERT INTO Vehicle_Categories (vehicle_type, hourly_rate)
VALUES ('2-Wheeler', 20.00), ('4-Wheeler', 50.00), ('Heavy', 100.00)
ON DUPLICATE KEY UPDATE vehicle_type = vehicle_type;

-- VIEWS

-- Readable list of the vehicles inside right now
-- upper-case plate, type, a Slot - N label and a friendly entry time, ordered by slot.
CREATE OR REPLACE VIEW Active_Parking_View AS
SELECT
    a.ticket_id, UPPER(a.vehicle_no) AS Reg_No, c.vehicle_type,
    CONCAT('Slot - ', a.slot_no) AS Assigned_Slot,
    DATE_FORMAT(a.entry_time, '%h:%i %p, %d %b') AS Entry_Timestamp
FROM Active_Parking a
INNER JOIN Vehicles v ON a.vehicle_no = v.vehicle_no
INNER JOIN Vehicle_Categories c ON v.category_id = c.category_id
ORDER BY a.slot_no ASC;

-- FUNCTIONS, TRIGGERS AND STORED PROCEDURES

-- DELIMITER is a mysql CLIENT command, not SQL. It changes the statement ender from
-- the semicolon to a double dollar sign, so the semicolons inside the routine bodies
-- below do not end the CREATE statement too early.
DELIMITER $$

-- Cleans a typed plate: trims it, removes spaces, dashes and dots, and upper-cases it.
-- DETERMINISTIC = same input always gives the same output. NO SQL = it reads no tables.
-- using these allows the database to perform an obscene amount of optimizations
-- and misusing these gives you datacorruption ;)
DROP FUNCTION IF EXISTS Normalize_Plate$$
CREATE FUNCTION Normalize_Plate(p_raw VARCHAR(50))
RETURNS VARCHAR(50) DETERMINISTIC NO SQL
BEGIN
    RETURN UPPER(REPLACE(REPLACE(REPLACE(TRIM(p_raw), ' ', ''), '-', ''), '.', ''));
END$$

-- The fee rules. Up to 15 minutes (900 seconds) is free. Anything longer is rounded
-- UP to whole hours and multiplied by the hourly rate, so 61 minutes is billed as 2 hours.
-- A NULL time gives a NULL difference, which is treated as free.
DROP FUNCTION IF EXISTS Calculate_Parking_Fee$$
CREATE FUNCTION Calculate_Parking_Fee(v_entry DATETIME, v_exit DATETIME, v_hourly_rate DECIMAL(8, 2))
RETURNS DECIMAL(10, 2) DETERMINISTIC NO SQL
BEGIN
    DECLARE total_secs INT;
    DECLARE computed_hours INT;
    DECLARE final_fee DECIMAL(10, 2);
    SET total_secs = TIMESTAMPDIFF(SECOND, v_entry, v_exit);
    CASE
        WHEN total_secs IS NULL OR total_secs <= 900 THEN SET final_fee = 0.00;
        ELSE
            SET computed_hours = CEIL(total_secs / 3600.0);
            SET final_fee = computed_hours * v_hourly_rate;
    END CASE;
    RETURN final_fee;
END$$

-- Runs before every insert into Active_Parking. When the lot already holds
-- 3000 vehicles it refuses the insert by raising a user defined error (SQLSTATE 45000).
DROP TRIGGER IF EXISTS trg_Before_Vehicle_Entry$$
CREATE TRIGGER trg_Before_Vehicle_Entry BEFORE INSERT ON Active_Parking FOR EACH ROW
BEGIN
    DECLARE active_count INT;
    SELECT COUNT(*) INTO active_count FROM Active_Parking;
    IF active_count >= 3000 THEN
        SIGNAL SQLSTATE '45000' SET MESSAGE_TEXT = 'Parking is FULL! Cannot issue ticket.';
    END IF;
END$$

-- Keeps Revenue_Audit in step with Parking_History (insert)
--  add the new fee to the row of the exit date
-- The first checkout of a day creates that row.
DROP TRIGGER IF EXISTS trg_After_History_Insert$$
CREATE TRIGGER trg_After_History_Insert AFTER INSERT ON Parking_History FOR EACH ROW
BEGIN
    INSERT INTO Revenue_Audit (audit_date, total_collection)
    VALUES (DATE(NEW.exit_time), NEW.total_fee)
    ON DUPLICATE KEY UPDATE total_collection = total_collection + NEW.total_fee;
END$$

-- Keeps Revenue_Audit in step (update): take the OLD fee away from the OLD date, 
-- add the NEW fee to the NEW date. The exit date can change too, so both are handled.
DROP TRIGGER IF EXISTS trg_After_History_Update$$
CREATE TRIGGER trg_After_History_Update AFTER UPDATE ON Parking_History FOR EACH ROW
BEGIN
    UPDATE Revenue_Audit
    SET total_collection = total_collection - OLD.total_fee
    WHERE audit_date = DATE(OLD.exit_time);
    INSERT INTO Revenue_Audit (audit_date, total_collection)
    VALUES (DATE(NEW.exit_time), NEW.total_fee)
    ON DUPLICATE KEY UPDATE total_collection = total_collection + NEW.total_fee;
END$$

-- Keeps Revenue_Audit in step (delete): take the deleted fee back out of that day.
DROP TRIGGER IF EXISTS trg_After_History_Delete$$
CREATE TRIGGER trg_After_History_Delete AFTER DELETE ON Parking_History FOR EACH ROW
BEGIN
    UPDATE Revenue_Audit
    SET total_collection = total_collection - OLD.total_fee
    WHERE audit_date = DATE(OLD.exit_time);
END$$

-- Checks a vehicle out inside ONE transaction find and lock its ticket, work out the fee 
-- copy the stay into Parking_History (the insert trigger then adds it to the revenue) 
-- and free the slot by deleting the ticket. Either all of it happens or none.(atomicccccitysatnniseani)
DROP PROCEDURE IF EXISTS sp_Checkout_Vehicle$$
CREATE PROCEDURE sp_Checkout_Vehicle(IN input_vehicle_no VARCHAR(50))
BEGIN
    DECLARE var_plate VARCHAR(50);
    DECLARE var_ticket_id INT;
    DECLARE var_slot_no INT;
    DECLARE var_entry_time DATETIME;
    DECLARE var_rate DECIMAL(8, 2);
    DECLARE out_time DATETIME;
    DECLARE computed_fee DECIMAL(10, 2);

    -- any SQL error: undo the whole transaction and report it
    DECLARE EXIT HANDLER FOR SQLEXCEPTION
    BEGIN
        ROLLBACK;
        SELECT 'ERROR: Transaction Rolled Back. Data Issue.' AS System_Message;
    END;

    -- clean the typed plate the same way the park procedure does
    SET var_plate = Normalize_Plate(input_vehicle_no);

    START TRANSACTION;
    -- FOR UPDATE locks the ticket row so two checkouts cannot work on it at the same time.
    -- When no row matches, the variables simply stay NULL.
    SELECT ticket_id, slot_no, entry_time
    INTO var_ticket_id, var_slot_no, var_entry_time
    FROM Active_Parking
    WHERE vehicle_no = var_plate
    FOR UPDATE;

    IF var_ticket_id IS NULL THEN
        -- nothing was found, so there is nothing to undo or commit
        ROLLBACK;
        SELECT 'Vehicle not found in active parking!' AS System_Message;
    ELSE
        -- hourly rate of this vehicle category
        SELECT c.hourly_rate INTO var_rate
        FROM Vehicles v
        INNER JOIN Vehicle_Categories c ON v.category_id = c.category_id
        WHERE v.vehicle_no = var_plate;

        -- NOW() is read once and stored, so the history row and the fee share the same moment
        SET out_time = NOW();
        SET computed_fee = Calculate_Parking_Fee(var_entry_time, out_time, var_rate);
        INSERT INTO Parking_History (vehicle_no, slot_no, entry_time, exit_time, total_fee)
        VALUES (var_plate, var_slot_no, var_entry_time, out_time, computed_fee);
        DELETE FROM Active_Parking WHERE ticket_id = var_ticket_id;
        COMMIT;
        SELECT 'Checkout Successful!' AS System_Message, computed_fee AS Total_Bill_Amt;
    END IF;
END$$

-- Parks a vehicle inside ONE transaction: validate the input, register the vehicle
-- (or update its category if the plate is known) and issue the ticket.
-- The handlers turn database errors into friendly messages.
DROP PROCEDURE IF EXISTS sp_Park_Vehicle$$
CREATE PROCEDURE sp_Park_Vehicle(IN p_plate VARCHAR(50), IN p_type VARCHAR(20), IN p_slot INT)
BEGIN
    DECLARE v_cat_id INT;
    DECLARE v_msg TEXT;

    -- When two handlers could catch an error, the most specific one wins:
    -- error code first, then SQLSTATE, then the general SQLEXCEPTION.

    -- Error 1062 = duplicate key. Either the plate is already parked or the slot is
    -- taken. The handler checks which one, so it can tell the user.
    DECLARE EXIT HANDLER FOR 1062
    BEGIN
        ROLLBACK;
        IF EXISTS (SELECT 1 FROM Active_Parking WHERE vehicle_no = p_plate) THEN
            SELECT 'vehicle is already currently parked!' AS System_Message;
        ELSEIF EXISTS (SELECT 1 FROM Active_Parking WHERE slot_no = p_slot) THEN
            SELECT 'this parking slot is already occupied!' AS System_Message;
        ELSE
            SELECT 'ERROR: Transaction Rolled Back. Data Issue.' AS System_Message;
        END IF;
    END;

    -- SQLSTATE 45000 = the error raised by SIGNAL in the full-lot trigger.
    -- GET DIAGNOSTICS copies the trigger message text so it can be shown.
    DECLARE EXIT HANDLER FOR SQLSTATE '45000'
    BEGIN
        GET DIAGNOSTICS CONDITION 1 v_msg = MESSAGE_TEXT;
        ROLLBACK;
        SELECT v_msg AS System_Message;
    END;

    -- any other SQL error: undo everything and show a general message
    DECLARE EXIT HANDLER FOR SQLEXCEPTION
    BEGIN
        ROLLBACK;
        SELECT 'ERROR: Transaction Rolled Back. Data Issue.' AS System_Message;
    END;

    -- clean the typed plate (an IN parameter can be reassigned inside the procedure)
    SET p_plate = Normalize_Plate(p_plate);

    -- check the input first, before any table is touched
    IF p_plate IS NULL OR p_plate NOT REGEXP '^[A-Z0-9]{4,20}$' THEN
        SELECT 'invalid vehicle number' AS System_Message;
    ELSEIF p_slot IS NULL OR p_slot NOT BETWEEN 1 AND 3000 THEN
        SELECT 'invalid slot number (must be 1 to 3000)' AS System_Message;
    ELSE
        -- v_cat_id stays NULL when the vehicle type does not exist
        SELECT category_id INTO v_cat_id FROM Vehicle_Categories WHERE vehicle_type = p_type;
        IF v_cat_id IS NULL THEN
            SELECT 'unknown vehicle type' AS System_Message;
        ELSE
            START TRANSACTION;
            -- new plate: add it. Known plate: just update its category.
            INSERT INTO Vehicles (vehicle_no, category_id) VALUES (p_plate, v_cat_id)
            ON DUPLICATE KEY UPDATE category_id = VALUES(category_id);
            -- issue the ticket. UNIQUE keys and the full-lot trigger can fail here and
            -- send us to the handlers above.
            INSERT INTO Active_Parking (vehicle_no, slot_no) VALUES (p_plate, p_slot);
            COMMIT;
            SELECT CONCAT('vehicle parked successfully in slot ', p_slot, '!') AS System_Message;
        END IF;
    END IF;
END$$

-- back to the normal semicolon ender
DELIMITER ;

-- Live running bill for every parked vehicle: minutes parked so far and the fee if the
-- vehicle left right now. Created after Calculate_Parking_Fee because it calls it.
-- This is what the estimate cost menu option reads.
CREATE OR REPLACE VIEW Active_Parking_Fee_View AS
SELECT
    a.vehicle_no AS Plate, c.hourly_rate AS Rate,
    TIMESTAMPDIFF(MINUTE, a.entry_time, NOW()) AS Mins_Parked,
    Calculate_Parking_Fee(a.entry_time, NOW(), c.hourly_rate) AS Estimated_Fee
FROM Active_Parking a
INNER JOIN Vehicles v ON a.vehicle_no = v.vehicle_no
INNER JOIN Vehicle_Categories c ON v.category_id = c.category_id;
"""

DEMO_SQL = r"""
-- SANITY CHECK ONLY. Moves the entry time of ONE parked vehicle back by some hours, so a long
-- stay (and its fee) can be tried without waiting. The old version had no WHERE and
-- moved every ticket at once.
DELIMITER $$

DROP PROCEDURE IF EXISTS sp_Backdate_Ticket$$
CREATE PROCEDURE sp_Backdate_Ticket(IN p_plate VARCHAR(50), IN p_hours INT)
BEGIN
    -- keep the shift between 1 hour and 30 days
    IF p_hours IS NULL OR p_hours < 1 OR p_hours > 720 THEN
        SELECT 'hours must be between 1 and 720' AS System_Message;
    ELSE
        -- only the ticket of this plate is changed (the plate is cleaned first)
        UPDATE Active_Parking
        SET entry_time = DATE_SUB(entry_time, INTERVAL p_hours HOUR)
        WHERE vehicle_no = Normalize_Plate(p_plate);
        -- ROW_COUNT() = number of rows the UPDATE just changed, 0 means no such ticket
        IF ROW_COUNT() = 0 THEN
            SELECT 'Vehicle not found in active parking!' AS System_Message;
        ELSE
            SELECT CONCAT('ticket moved back by ', p_hours, ' hour(s) - TEST ONLY') AS System_Message;
        END IF;
    END IF;
END$$

DELIMITER ;
"""

SQL_PARK = "CALL sp_Park_Vehicle(%s, %s, %s)"
SQL_CHECKOUT = "CALL sp_Checkout_Vehicle(%s)"
SQL_ESTIMATE_FEE = (
    "SELECT Plate, Rate, Mins_Parked, Estimated_Fee "
    "FROM Active_Parking_Fee_View WHERE Plate = Normalize_Plate(%s)"
)
SQL_BACKDATE = "CALL sp_Backdate_Ticket(%s, %s)"

# ENUMS

class EnumVehicleCategory(str, Enum):
    TWO_WHEELER = "2-Wheeler"
    FOUR_WHEELER = "4-Wheeler"
    HEAVY = "Heavy"

class EnumMenuAction(int, Enum):
    PARK_VEHICLE = 1
    EXIT_VEHICLE = 2
    ESTIMATE_COST = 3
    FAST_FORWARD_TIME = 4
    DISPLAY_GRID = 5
    DISPLAY_SETTINGS = 6
    EXIT_PROGRAM = 7

class EnumArtStyleType(str, Enum):
    CARS = "CARS"
    DOTS = "DOTS"

# We store the ASCII art styles
DICT_ASCII_ART_STYLES = {
    EnumArtStyleType.CARS: {
        "name_str": "ascii cars",
        "desc_str": "bay with a car and the number plate inside",
        "slot_header_fmt": "SLOT {number}",
        "gate_header_str": "ENTRY/EXIT",
        "lines_empty_tuple": (
            r"+----------------------+",
            r"|                      |",
            r"|                      |",
            r"|{green}     - available -    {reset}|",
            r"|                      |",
            r"|                      |",
            r"+----------------------+",
        ),
        "lines_taken_tuple": (
            r"+----------------------+",
            r"|       ________       |",
            r"|    __/  |  |  \__    |",
            r"| /{yellow}[]{reset}              {yellow}[]{reset}\ |",
            r"| \_{red}(O){reset}__________{red}(O){reset}_/ |",
            r"|{cyan}{plate}{reset}|",
            r"+----------------------+",
        ),
        "lines_gate_tuple": (
            r"+----------------------+",
            r"|                      |",
            r"|{yellow}    >>> ENTRY >>>     {reset}|",
            r"|                      |",
            r"|{yellow}    <<<  EXIT <<<     {reset}|",
            r"|                      |",
            r"+----------------------+",
        ),
        "plate_width_int": 22,
        "gap_str": " ",
        "legend_str": "",
    },
    EnumArtStyleType.DOTS: {
        "name_str": "red dots",
        "desc_str": "tiny bordered cell, one red O when taken, blank when free",
        "slot_header_fmt": "{number}",
        "gate_header_str": "I/O",
        "lines_empty_tuple": (
            r"+---+",
            r"|   |",
            r"+---+",
        ),
        "lines_taken_tuple": (
            r"+---+",
            r"| {red}O{reset} |",
            r"+---+",
        ),
        "lines_gate_tuple": (
            r"+---+",
            r"|{yellow}I/O{reset}|",
            r"+---+",
        ),
        "plate_width_int": 0,
        "gap_str": " ",
        "legend_str": "{red}O{reset} = taken    blank = free    {yellow}I/O{reset} = entry/exit gate",
    }
}

# UTILITIES & LOGGING

def util_log_debug(str_message: str) -> None:
    # We only print debug messages if the global IS_DEBUG flag is set to True.
    if IS_DEBUG:
        print(f"{Fore.MAGENTA}[DEBUG] {str_message}{Style.RESET_ALL}")

def util_escape_sql_string(any_value: Any) -> str:
    # We convert Python values into safe SQL literals to prevent injection and syntax errors.
    if any_value is None:
        return "NULL"
    if isinstance(any_value, bool):
        raise TypeError("A boolean is not a valid SQL parameter in this context.")
    if isinstance(any_value, int):
        return str(any_value)
    if isinstance(any_value, str):
        # We iterate over our predefined escape sequences and replace them sequentially.
        for str_raw_char, str_escaped_char in CONST_SQL_STRING_ESCAPE_SEQUENCE:
            any_value = any_value.replace(str_raw_char, str_escaped_char)
        return "'" + any_value + "'"
    raise TypeError(f"Cannot use {type(any_value).__name__} as a SQL parameter.")

def util_bind_sql_parameters(str_sql_template: str, tuple_parameters: tuple) -> str:
    # We split the template at every '%s' placeholder.
    list_of_sql_pieces = str_sql_template.split("%s")
    
    # We validate that the number of placeholders matches the number of provided parameters.
    if len(list_of_sql_pieces) - 1 != len(tuple_parameters):
        raise ValueError(f"SQL statement has {len(list_of_sql_pieces) - 1} placeholder(s) but {len(tuple_parameters)} value(s) were given.")
    
    # We reconstruct the SQL string by interleaving the pieces with the escaped parameter values.
    str_final_sql_statement = list_of_sql_pieces[0]
    for any_value, str_piece in zip(tuple_parameters, list_of_sql_pieces[1:]):
        str_final_sql_statement += util_escape_sql_string(any_value) + str_piece
        
    return str_final_sql_statement

def util_strip_ansi_color_codes(str_art_line: str, int_plate_width: int) -> str:
    # We replace the plate token with empty spaces of the correct width.
    str_plain_text = str_art_line.replace(CONST_ASCII_PLATE_TOKEN, " " * int_plate_width)
    
    # We remove all color tokens so we can accurately measure the visible width of the string.
    for str_token in CONST_ASCII_COLOR_TOKENS:
        str_plain_text = str_plain_text.replace(str_token, "")
        
    return str_plain_text

def util_inject_ansi_color_codes(str_art_line: str, str_plate_text: str, int_plate_width: int) -> str:
    # We center the plate text within the allocated width and inject it into the token.
    str_colored_text = str_art_line.replace(CONST_ASCII_PLATE_TOKEN, str_plate_text[:int_plate_width].center(int_plate_width))
    
    # We replace our custom color tokens with actual ANSI escape codes.
    for str_token, str_color_code in CONST_ASCII_COLOR_TOKENS.items():
        str_colored_text = str_colored_text.replace(str_token, str_color_code)
        
    return str_colored_text

def util_parse_mysql_table_output(list_of_output_lines: list[str]) -> list[list[str]]:
    # We parse the ASCII table output from the MySQL client into a list of string lists.
    list_of_parsed_rows = []
    for str_line in list_of_output_lines:
        if str_line.startswith("|"):
            # We split by the pipe character, strip whitespace, and ignore the empty first/last elements.
            list_of_parsed_rows.append([str_cell.strip() for str_cell in str_line.strip("|").split("|")])
            
    # We return all rows except the first one, which is the header row.
    return list_of_parsed_rows[1:] if len(list_of_parsed_rows) > 1 else []

#  RENDERING

def pure_render_ascii_parking_grid(
    dict_parked_slots: dict[int, str],
    dict_selected_style: dict,
    int_column_count: int,
    bool_include_gate: bool
) -> list[str]:
    # We calculate the visible width and height of a single cell based on the empty slot template.
    int_cell_width = len(util_strip_ansi_color_codes(dict_selected_style["lines_empty_tuple"][0], dict_selected_style["plate_width_int"]))
    int_cell_height = len(dict_selected_style["lines_empty_tuple"])
    
    # We build a list of tuples representing every cell we need to draw (type and slot number).
    list_of_cells_to_draw = []
    for int_slot_number in range(1, CONST_TOTAL_PARKING_SLOTS + 1):
        str_cell_kind = "taken" if int_slot_number in dict_parked_slots else "free"
        list_of_cells_to_draw.append((str_cell_kind, int_slot_number))
        
    if bool_include_gate:
        list_of_cells_to_draw.append(("gate", 0))

    list_of_rendered_grid_lines = []
    
    # We iterate over the cells in chunks, where each chunk represents one row in the grid.
    for int_row_start_index in range(0, len(list_of_cells_to_draw), int_column_count):
        list_of_row_cells = list_of_cells_to_draw[int_row_start_index : int_row_start_index + int_column_count]

        # We construct the header line for this row.
        list_of_header_strings = []
        for str_kind, int_number in list_of_row_cells:
            if str_kind == "gate":
                str_header_text = dict_selected_style["gate_header_str"]
            else:
                str_header_text = dict_selected_style["slot_header_fmt"].format(number=int_number)
            str_centered_header = str_header_text[:int_cell_width].center(int_cell_width)
            list_of_header_strings.append(f"{Style.BRIGHT}{str_centered_header}{Style.RESET_ALL}")
            
        list_of_rendered_grid_lines.append(dict_selected_style["gap_str"].join(list_of_header_strings))

        # We construct the actual ASCII art lines for this row.
        list_of_drawn_cells = []
        for str_kind, int_number in list_of_row_cells:
            str_plate = dict_parked_slots.get(int_number, "")
            if str_kind == "free":
                tuple_art_lines = dict_selected_style["lines_empty_tuple"]
            elif str_kind == "taken":
                tuple_art_lines = dict_selected_style["lines_taken_tuple"]
            else:
                tuple_art_lines = dict_selected_style["lines_gate_tuple"]
                
            list_of_colored_lines = [util_inject_ansi_color_codes(str_line, str_plate, dict_selected_style["plate_width_int"]) for str_line in tuple_art_lines]
            list_of_drawn_cells.append(list_of_colored_lines)

        # We interleave the lines of each cell side-by-side to form the final text rows.
        for int_line_index in range(int_cell_height):
            list_of_interleaved_strings = [list_of_cell_lines[int_line_index] for list_of_cell_lines in list_of_drawn_cells]
            list_of_rendered_grid_lines.append(dict_selected_style["gap_str"].join(list_of_interleaved_strings))
            
        # We append an empty line to separate grid rows.
        list_of_rendered_grid_lines.append("")
        
    return list_of_rendered_grid_lines

# DATABASE EXECUTION PROCEDURES

def proc_execute_raw_sql_query(obj_client_process: subprocess.Popen, str_raw_sql_query: str) -> list[str]:
    # We write the raw SQL query followed by our special marker query to the client's standard input.
    try:
        obj_client_process.stdin.write(str_raw_sql_query + "\n" + CONST_SQL_END_MARKER_QUERY + "\n")
        obj_client_process.stdin.flush()
    except OSError as obj_error:
        raise RuntimeError(f"Cannot write to the database client process. Connection lost: {obj_error}")

    list_of_output_lines = []
    
    # We read lines from the client's standard output until we see our marker column.
    while True:
        str_line = obj_client_process.stdout.readline()
        
        # An empty string indicates the stream was closed (the client process exited unexpectedly).
        if str_line == "":
            raise RuntimeError("Database client process exited unexpectedly. Output: " + " | ".join(list_of_output_lines))
            
        str_line = str_line.rstrip("\r\n")

        # If we detect our marker column, we know the query has finished executing.
        if CONST_SQL_END_MARKER_COLUMN in str_line:
            break
            
        # We skip lines that are known to be harmless warnings.
        bool_ignore_line = False
        for str_ignored_text in CONST_IGNORED_CLIENT_OUTPUT_LINES:
            if str_ignored_text in str_line:
                bool_ignore_line = True
                break
                
        if bool_ignore_line:
            continue
            
        list_of_output_lines.append(str_line)

    # The marker query prints a 5-line ASCII table. We pop the top border line that was already appended.
    if list_of_output_lines:
        list_of_output_lines.pop()
        
    # We consume the remaining 3 lines of the marker table from the stdout pipe to clear it.
    for _ in range(3):
        obj_client_process.stdout.readline()
        
    return list_of_output_lines

def proc_execute_sql_and_print(obj_client_process: subprocess.Popen, str_raw_sql_query: str) -> list[str]:
    # We optionally print the SQL query to the terminal for debugging or demonstration purposes.
    if SHOW_SQL:
        print(f"{Fore.CYAN}sql> {str_raw_sql_query}{Style.RESET_ALL}")
        
    # We execute the query and fetch the output lines.
    list_of_output_lines = proc_execute_raw_sql_query(obj_client_process, str_raw_sql_query)
    
    # We print each line of the output exactly as the MySQL client formatted it.
    for str_line in list_of_output_lines:
        print(str_line)
        
    # If there was no output, we print "Empty set" to mimic standard MySQL client behavior.
    if not list_of_output_lines:
        print("Empty set")
        
    return list_of_output_lines

def proc_execute_parameterized_sql_and_print(obj_client_process: subprocess.Popen, str_sql_template: str, tuple_parameters: tuple) -> list[str]:
    # We bind the parameters to the template, append a semicolon, and execute it.
    str_final_query = util_bind_sql_parameters(str_sql_template, tuple_parameters) + ";"
    return proc_execute_sql_and_print(obj_client_process, str_final_query)


def app_main_execution_loop() -> None:
    # We initialize colorama to ensure ANSI escape codes render correctly on Windows.
    init_colorama()
    
    # We define a signal handler to raise a SystemExit exception when the process is terminated.
    # This ensures our 'finally' block will execute and clean up the database processes.
    def proc_handle_termination_signal(int_signal_number, obj_frame) -> None:
        util_log_debug(f"Received termination signal {int_signal_number}. Raising SystemExit.")
        raise SystemExit(1)
        
    signal.signal(signal.SIGTERM, proc_handle_termination_signal)
    if not IS_WINDOWS:
        signal.signal(signal.SIGHUP, proc_handle_termination_signal)

    print("         SMART PARKING MANAGEMENT SYSTEM")
    print("         starting database engine...\n")

    # We declare our state variables for the database processes so they are accessible in the finally block.
    obj_db_server_process: Optional[subprocess.Popen] = None
    obj_db_client_process: Optional[subprocess.Popen] = None
    obj_db_server_log_file = None
    path_unix_socket: Optional[Path] = None

    try:
        
        # SUBROUTINE: LOCATE DATABASE EXECUTABLES
        util_log_debug("Locating database executables in system PATH.")
        str_system_path = os.environ.get("PATH", "")
        if not IS_WINDOWS:
            str_system_path += os.pathsep + os.pathsep.join(CONST_EXTRA_LINUX_PROGRAM_FOLDERS)
            
        path_server_executable = None
        for str_name in ["mariadbd", "mysqld"]:
            str_found_path = shutil.which(str_name, path=str_system_path)
            if str_found_path:
                path_server_executable = Path(str_found_path)
                break
                
        if path_server_executable is None:
            raise RuntimeError("No mysqld or mariadbd found in PATH. Install MySQL or MariaDB first.")

        # We run the server executable with --version to determine if it is MariaDB or MySQL.
        obj_version_run = subprocess.run(
            [str(path_server_executable), "--version"], 
            capture_output=True, 
            text=True, 
            timeout=15,
            creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
        )
        bool_is_mariadb = "mariadb" in (obj_version_run.stdout + obj_version_run.stderr).lower()

        path_client_executable = None
        list_of_client_names = ["mariadb", "mysql"] if bool_is_mariadb else ["mysql", "mariadb"]
        for str_name in list_of_client_names:
            str_found_path = shutil.which(str_name, path=str_system_path)
            if str_found_path:
                path_client_executable = Path(str_found_path)
                break
                
        if path_client_executable is None:
            raise RuntimeError("Found a server but no mysql/mariadb command line client in PATH.")

        path_install_script = None
        if bool_is_mariadb:
            for str_name in ["mariadb-install-db", "mysql_install_db"]:
                str_found_path = shutil.which(str_name, path=str_system_path)
                if str_found_path:
                    path_install_script = Path(str_found_path)
                    break
            if path_install_script is None:
                raise RuntimeError("MariaDB found but mariadb-install-db is missing from PATH.")

        print(f"using {'MariaDB' if bool_is_mariadb else 'MySQL'} server: {path_server_executable}")
        print(f"using client           : {path_client_executable}")

        # SUBROUTINE: CLEANUP LEFTOVER PROCESSES
        util_log_debug("Checking for leftover database processes from previous crashed runs.")
        CONST_DATA_DIRECTORY.mkdir(parents=True, exist_ok=True)
        
        if CONST_SERVER_PID_FILE.exists():
            try:
                int_old_pid = int(CONST_SERVER_PID_FILE.read_text().split()[0])
                bool_is_our_process = False
                
                if IS_WINDOWS:
                    obj_task_list = subprocess.run(
                        ["tasklist", "/FI", f"PID eq {int_old_pid}", "/FO", "CSV", "/NH"],
                        capture_output=True, text=True, creationflags=subprocess.CREATE_NO_WINDOW
                    )
                    bool_is_our_process = "mysqld" in obj_task_list.stdout.lower() or "mariadbd" in obj_task_list.stdout.lower()
                else:
                    str_command_line = Path(f"/proc/{int_old_pid}/cmdline").read_bytes().decode(errors="replace")
                    bool_is_our_process = CONST_DATABASE_DIRECTORY.as_posix() in str_command_line
                    
                if bool_is_our_process:
                    print(f"found a leftover database process (pid {int_old_pid}) from a crashed run, killing it...")
                    if IS_WINDOWS:
                        subprocess.run(["taskkill", "/F", "/T", "/PID", str(int_old_pid)], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                    else:
                        os.kill(int_old_pid, signal.SIGKILL)
                    time.sleep(1)
            except Exception as obj_err:
                util_log_debug(f"Failed to kill leftover process: {obj_err}")
                
            CONST_SERVER_PID_FILE.unlink(missing_ok=True)

        # SUBROUTINE: FIND FREE PORT & SOCKET PATH
        util_log_debug("Finding an available TCP port.")
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as obj_probe_socket:
            obj_probe_socket.bind((DATABASE_HOST, 0))
            int_database_port = obj_probe_socket.getsockname()[1]
            
        if not IS_WINDOWS:
            path_preferred_socket = CONST_DATA_DIRECTORY / "mysql.sock"
            if len(str(path_preferred_socket)) <= CONST_UNIX_SOCKET_MAX_PATH_LENGTH:
                path_unix_socket = path_preferred_socket
            else:
                path_unix_socket = Path(tempfile.gettempdir()) / f"smart_parking_{int_database_port}.sock"
            path_unix_socket.unlink(missing_ok=True)

        # SUBROUTINE: INITIALIZE DATABASE DIRECTORY (FIRST LAUNCH)
        # We determine if this is the first launch by checking for the marker file or missing DB directory.

        bool_is_first_launch = not CONST_FIRST_LAUNCH_MARKER_FILE.exists() or not CONST_DATABASE_DIRECTORY.exists()
        
        if bool_is_first_launch:
            print("first launch: setting up the private data folder...")
            CONST_FIRST_LAUNCH_MARKER_FILE.unlink(missing_ok=True)
            shutil.rmtree(CONST_DATABASE_DIRECTORY, ignore_errors=True)
            
            str_data_folder_flag = f"--datadir={CONST_DATABASE_DIRECTORY.as_posix()}"
            
            try:
                str_current_username = os.environ.get("USER", "root")
            except Exception:
                str_current_username = "root"
                
            if bool_is_mariadb:
                list_init_command = [str(path_install_script)]
                if not IS_WINDOWS:
                    list_init_command += ["--no-defaults", f"--basedir={path_server_executable.parent.parent.as_posix()}", f"--user={str_current_username}"]
                list_init_command.append(str_data_folder_flag)
            else:
                list_init_command = [str(path_server_executable), "--no-defaults", "--initialize-insecure", str_data_folder_flag]
                if not IS_WINDOWS:
                    list_init_command.append(f"--user={str_current_username}")
                    
            util_log_debug(f"Running init command: {list_init_command}")
            obj_init_run = subprocess.run(
                list_init_command, check=True, capture_output=True, text=True, timeout=180,
                creationflags=subprocess.CREATE_NO_WINDOW if IS_WINDOWS else 0
            )
            
            CONST_ROOT_SETUP_SQL_FILE.write_text(ROOT_SETUP_SQL, encoding="utf-8")

        # SUBROUTINE: START DATABASE SERVER PROCESS
        print(f"starting database server on port {int_database_port}...")
        list_server_command = [
            str(path_server_executable),
            "--no-defaults",
            f"--datadir={CONST_DATABASE_DIRECTORY.as_posix()}",
            f"--port={int_database_port}",
            f"--bind-address={DATABASE_HOST}",
            f"--pid-file={CONST_SERVER_PID_FILE.as_posix()}",
            "--skip-name-resolve",
        ]
        
        if IS_WINDOWS:
            list_server_command.append("--console")
        else:
            list_server_command.append(f"--socket={path_unix_socket.as_posix()}")
            try:
                list_server_command.append(f"--user={os.environ.get('USER', 'root')}")
            except Exception:
                pass
                
        list_server_command += SLIM_SERVER_FLAGS
        if bool_is_mariadb:
            list_server_command += MARIADB_LIKE_MYSQL_FLAGS
        else:
            list_server_command.append("--loose-mysqlx=OFF")
            
        if bool_is_first_launch:
            list_server_command.append(f"--init-file={CONST_ROOT_SETUP_SQL_FILE.as_posix()}")
            
        util_log_debug(f"Starting server with command: {list_server_command}")
        
        obj_db_server_log_file = open(CONST_SERVER_LOG_FILE, "wb")
        
        dict_process_options = {}
        if IS_WINDOWS:
            dict_process_options["creationflags"] = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
        else:
            dict_process_options["start_new_session"] = True
            
        obj_db_server_process = subprocess.Popen(
            list_server_command, stdout=obj_db_server_log_file, stderr=subprocess.STDOUT, **dict_process_options
        )

        # SUBROUTINE: WAIT FOR SERVER TO ACCEPT CONNECTIONS
        print("waiting for database engine...", end="", flush=True)
        bool_server_ready = False
        str_last_connection_error = ""
        
        for _ in range(CONST_STARTUP_TIMEOUT_SECONDS):
            if obj_db_server_process.poll() is not None:
                print(f"\nFatal Error: Database server crashed during startup (exit code {obj_db_server_process.returncode}).")
                raise RuntimeError("Database server crashed.")
                
            list_client_command = [
                str(path_client_executable),
                "--no-defaults",
                "--protocol=TCP",
                f"--host={DATABASE_HOST}",
                f"--port={int_database_port}",
                f"--user={DATABASE_USER}",
                f"--password={DATABASE_PASSWORD}",
                "--table",
                "--unbuffered",
                "--force",
                "--default-character-set=utf8mb4",
            ]
            
            try:
                obj_temp_client_process = subprocess.Popen(
                    list_client_command,
                    stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                    text=True, encoding="utf-8", errors="replace", **dict_process_options
                )
                proc_execute_raw_sql_query(obj_temp_client_process, "SELECT 1;")
                
                # If we reach here, the connection was successful. We keep this client process open.
                obj_db_client_process = obj_temp_client_process
                bool_server_ready = True
                print(" ready!")
                break
            except Exception as obj_err:
                str_last_connection_error = str(obj_err)
                if 'obj_temp_client_process' in locals() and obj_temp_client_process:
                    try:
                        obj_temp_client_process.stdin.close()
                        obj_temp_client_process.kill()
                    except Exception:
                        pass
                print(".", end="", flush=True)
                time.sleep(1)
                
        if not bool_server_ready:
            print(f"\nFatal Error: Database did not accept connections in time. Last error: {str_last_connection_error}")
            raise RuntimeError("Database connection timeout.")

        # SUBROUTINE: CREATE TABLES AND PROCEDURES
        print("creating tables, view, function, triggers and procedures...")
        str_init_sql_batch = f"CREATE DATABASE IF NOT EXISTS {DATABASE_NAME};\nUSE {DATABASE_NAME};\n{SCHEMA_SQL}\n{DEMO_SQL}"
        list_init_output_lines = proc_execute_raw_sql_query(obj_db_client_process, str_init_sql_batch)
        
        list_error_lines = [str_line for str_line in list_init_output_lines if str_line.startswith("ERROR")]
        if list_error_lines:
            raise RuntimeError("Setting up the tables failed:\n" + "\n".join(list_error_lines))
            
        if bool_is_first_launch:
            CONST_FIRST_LAUNCH_MARKER_FILE.write_text("done", encoding="utf-8")
            CONST_ROOT_SETUP_SQL_FILE.unlink(missing_ok=True)

        print("database connected successfully!")

        # MAIN MENU LOOP (STATE MACHINE???)
        
        # We define the initial UI state variables.
        enum_current_art_style = EnumArtStyleType.CARS
        int_current_grid_columns = CONST_AUTO_COLUMNS_FLAG

        while True:
            # We print the main menu UI.
            print("\n" + "=" * 60)
            print(" DATABASE VIEWER CONNECTION INFO:")
            print(f" Host: {DATABASE_HOST}   |   Port: {int_database_port}")
            print(f" User: {DATABASE_USER}        |   Pass: {DATABASE_PASSWORD}")
            print("=" * 60)
            print("1. park vehicle")
            print("2. vehicle exit")
            print("3. estimate parking cost")
            print("4. fast forward time")
            print("5. display parking lot grid")
            print("6. display settings (change art / columns)")
            print("7. exit program")
            print("=" * 60)

            # We prompt the user for their menu choice.
            str_raw_choice = input("enter your choice (1-7): ").strip()
            if not str_raw_choice:
                continue
                
            try:
                int_choice = int(str_raw_choice)
                enum_selected_action = EnumMenuAction(int_choice)
            except ValueError:
                print("bad input. need a real number between 1 and 7.")
                continue

            # We use modern Python match-case to handle the explicit state transitions.
            match enum_selected_action:
                
                case EnumMenuAction.PARK_VEHICLE:
                    print("\n" + "-" * 60 + "\n" + "PARK VEHICLE".center(60) + "\n" + "-" * 60)
                    
                    # We fetch the currently parked vehicles to determine free slots.
                    list_parked_output = proc_execute_raw_sql_query(obj_db_client_process, "SELECT slot_no, vehicle_no FROM Active_Parking;")
                    list_parsed_parked_rows = util_parse_mysql_table_output(list_parked_output)
                    dict_active_parked_vehicles = {int(str_slot): str_plate for str_slot, str_plate in list_parsed_parked_rows}
                    
                    list_free_slots = [int_slot for int_slot in range(1, CONST_TOTAL_PARKING_SLOTS + 1) if int_slot not in dict_active_parked_vehicles]
                    if not list_free_slots:
                        print("\nparking lot is full!")
                        continue

                    # We prompt for the vehicle plate, enforcing a specific regex pattern.
                    obj_plate_pattern = re.compile(r"[A-Za-z0-9]{4,15}")
                    str_vehicle_plate = ""
                    while True:
                        str_input_plate = input("enter vehicle number : ").strip()
                        if not str_input_plate:
                            print("cannot be empty.")
                        elif not obj_plate_pattern.fullmatch(str_input_plate):
                            print("invalid plate. use 4-15 letters/numbers only.")
                        else:
                            str_vehicle_plate = str_input_plate.upper()
                            break

                    # We prompt for the vehicle type.
                    list_vehicle_types = [EnumVehicleCategory.TWO_WHEELER.value, EnumVehicleCategory.FOUR_WHEELER.value, EnumVehicleCategory.HEAVY.value]
                    print("vehicle types        : " + "   ".join(f"{int_idx}. {str_name}" for int_idx, str_name in enumerate(list_vehicle_types, start=1)))
                    str_vehicle_type = ""
                    while True:
                        str_input_type = input("enter vehicle type   : ").strip()
                        try:
                            int_type_choice = int(str_input_type)
                            if 1 <= int_type_choice <= len(list_vehicle_types):
                                str_vehicle_type = list_vehicle_types[int_type_choice - 1]
                                break
                            else:
                                print(f"too high/low. choose 1 to {len(list_vehicle_types)}.")
                        except ValueError:
                            print("bad input. need a real number.")

                    # We prompt for the parking slot.
                    print("free slots           : " + " ".join(str(int_slot) for int_slot in list_free_slots))
                    int_selected_slot = 0
                    while True:
                        str_input_slot = input(f"enter parking slot (1-{CONST_TOTAL_PARKING_SLOTS}): ").strip()
                        try:
                            int_slot_choice = int(str_input_slot)
                            if 1 <= int_slot_choice <= CONST_TOTAL_PARKING_SLOTS:
                                int_selected_slot = int_slot_choice
                                break
                            else:
                                print(f"too high/low. choose 1 to {CONST_TOTAL_PARKING_SLOTS}.")
                        except ValueError:
                            print("bad input. need a real number.")

                    print()
                    proc_execute_parameterized_sql_and_print(obj_db_client_process, SQL_PARK, (str_vehicle_plate, str_vehicle_type, int_selected_slot))

                case EnumMenuAction.EXIT_VEHICLE:
                    print("\n" + "-" * 60 + "\n" + "VEHICLE EXIT".center(60) + "\n" + "-" * 60)
                    
                    obj_plate_pattern = re.compile(r"[A-Za-z0-9]{4,15}")
                    str_vehicle_plate = ""
                    while True:
                        str_input_plate = input("enter vehicle number : ").strip()
                        if not str_input_plate:
                            print("cannot be empty.")
                        elif not obj_plate_pattern.fullmatch(str_input_plate):
                            print("invalid plate.")
                        else:
                            str_vehicle_plate = str_input_plate.upper()
                            break

                    print()
                    proc_execute_parameterized_sql_and_print(obj_db_client_process, SQL_CHECKOUT, (str_vehicle_plate,))

                case EnumMenuAction.ESTIMATE_COST:
                    print("\n" + "-" * 60 + "\n" + "ESTIMATE COST".center(60) + "\n" + "-" * 60)
                    
                    obj_plate_pattern = re.compile(r"[A-Za-z0-9]{4,15}")
                    str_vehicle_plate = ""
                    while True:
                        str_input_plate = input("enter vehicle number : ").strip()
                        if not str_input_plate:
                            print("cannot be empty.")
                        elif not obj_plate_pattern.fullmatch(str_input_plate):
                            print("invalid plate.")
                        else:
                            str_vehicle_plate = str_input_plate.upper()
                            break

                    print()
                    list_estimate_output = proc_execute_parameterized_sql_and_print(obj_db_client_process, SQL_ESTIMATE_FEE, (str_vehicle_plate,))
                    if not list_estimate_output:
                        print("vehicle not found in active parking!")

                case EnumMenuAction.FAST_FORWARD_TIME:
                    print("\n" + "-" * 60 + "\n" + "FAST FORWARD TIME".center(60) + "\n" + "-" * 60)
                    
                    obj_plate_pattern = re.compile(r"[A-Za-z0-9]{4,15}")
                    str_vehicle_plate = ""
                    while True:
                        str_input_plate = input("enter vehicle number : ").strip()
                        if not str_input_plate:
                            print("cannot be empty.")
                        elif not obj_plate_pattern.fullmatch(str_input_plate):
                            print("invalid plate.")
                        else:
                            str_vehicle_plate = str_input_plate.upper()
                            break

                    int_hours_to_forward = 0
                    while True:
                        str_input_hours = input("enter hours to fast forward (1-720): ").strip()
                        try:
                            int_hours_choice = int(str_input_hours)
                            if 1 <= int_hours_choice <= 720:
                                int_hours_to_forward = int_hours_choice
                                break
                            else:
                                print("too high/low. choose 1 to 720.")
                        except ValueError:
                            print("bad input. need a real number.")

                    print()
                    proc_execute_parameterized_sql_and_print(obj_db_client_process, SQL_BACKDATE, (str_vehicle_plate, int_hours_to_forward))

                case EnumMenuAction.DISPLAY_GRID:
                    # We fetch the currently parked vehicles.
                    list_parked_output = proc_execute_raw_sql_query(obj_db_client_process, "SELECT slot_no, vehicle_no FROM Active_Parking;")
                    list_parsed_parked_rows = util_parse_mysql_table_output(list_parked_output)
                    dict_active_parked_vehicles = {int(str_slot): str_plate for str_slot, str_plate in list_parsed_parked_rows}
                    
                    dict_selected_style = DICT_ASCII_ART_STYLES[enum_current_art_style]
                    
                    # We determine the actual number of columns to use.
                    if int_current_grid_columns == CONST_AUTO_COLUMNS_FLAG:
                        int_terminal_width = shutil.get_terminal_size(fallback=(100, 30)).columns
                        int_cell_width = len(util_strip_ansi_color_codes(dict_selected_style["lines_empty_tuple"][0], dict_selected_style["plate_width_int"]))
                        int_gap_width = len(dict_selected_style["gap_str"])
                        int_columns_that_fit = (int_terminal_width + int_gap_width) // (int_cell_width + int_gap_width)
                        int_resolved_columns = max(1, min(int_columns_that_fit, CONST_TOTAL_PARKING_SLOTS + 1))
                    else:
                        int_resolved_columns = int_current_grid_columns

                    # We calculate the total width of the grid to center it.
                    int_cell_width = len(util_strip_ansi_color_codes(dict_selected_style["lines_empty_tuple"][0], dict_selected_style["plate_width_int"]))
                    int_total_grid_width = int_resolved_columns * int_cell_width + (int_resolved_columns - 1) * len(dict_selected_style["gap_str"])
                    
                    print("\n" + "PARKING LOT".center(int_total_grid_width))
                    
                    list_of_rendered_lines = pure_render_ascii_parking_grid(dict_active_parked_vehicles, dict_selected_style, int_resolved_columns, True)
                    for str_line in list_of_rendered_lines:
                        print(str_line)

                    print(f"taken: {len(dict_active_parked_vehicles)} / {CONST_TOTAL_PARKING_SLOTS}    free: {CONST_TOTAL_PARKING_SLOTS - len(dict_active_parked_vehicles)}")
                    if dict_selected_style["legend_str"]:
                        # We inject color codes into the legend string before printing.
                        str_colored_legend = dict_selected_style["legend_str"]
                        for str_token, str_color_code in CONST_ASCII_COLOR_TOKENS.items():
                            str_colored_legend = str_colored_legend.replace(str_token, str_color_code)
                        print(str_colored_legend)
                        
                    int_terminal_width = shutil.get_terminal_size(fallback=(100, 30)).columns
                    if int_total_grid_width > int_terminal_width:
                        print(f"{Fore.YELLOW}note: grid is {int_total_grid_width} wide, terminal is {int_terminal_width}. lines will wrap, use fewer columns.{Style.RESET_ALL}")

                    print("\nActive_Parking_View:")
                    proc_execute_sql_and_print(obj_db_client_process, "SELECT * FROM Active_Parking_View;")
                    print("\nRevenue_Audit:")
                    proc_execute_sql_and_print(obj_db_client_process, "SELECT * FROM Revenue_Audit ORDER BY audit_date DESC;")

                case EnumMenuAction.DISPLAY_SETTINGS:
                    while True:
                        print("\n--- DISPLAY SETTINGS ---")
                        print("1. choose art style (ascii cars / red dots)")
                        print("2. choose grid columns")
                        print("3. go back to main menu")
                        
                        str_input_setting = input("choose option: ").strip()
                        try:
                            int_setting_choice = int(str_input_setting)
                        except ValueError:
                            print("bad input. need a real number.")
                            continue
                            
                        if int_setting_choice == 1:
                            print("\n--- ART STYLES ---")
                            list_of_styles = list(EnumArtStyleType)
                            for int_idx, enum_style in enumerate(list_of_styles, start=1):
                                dict_style_data = DICT_ASCII_ART_STYLES[enum_style]
                                print(f"\n{int_idx}. {dict_style_data['name_str']}  -  {dict_style_data['desc_str']}")
                                list_preview_lines = pure_render_ascii_parking_grid({2: "MH12AB1234"}, dict_style_data, 2, False)
                                # We only print the first few lines of the preview to save space.
                                for str_line in list_preview_lines[:dict_style_data["lines_empty_tuple"].__len__() + 1]:
                                    print("   " + str_line)
                                    
                            str_input_style = input(f"\nchoose style (1-{len(list_of_styles)}): ").strip()
                            try:
                                int_style_choice = int(str_input_style)
                                if 1 <= int_style_choice <= len(list_of_styles):
                                    enum_current_art_style = list_of_styles[int_style_choice - 1]
                                    print(f"switched to {DICT_ASCII_ART_STYLES[enum_current_art_style]['name_str']}.")
                                else:
                                    print("invalid choice.")
                            except ValueError:
                                print("bad input.")
                                
                        elif int_setting_choice == 2:
                            print(f"\n--- COLUMNS ---")
                            str_input_cols = input(f"choose columns ({CONST_AUTO_COLUMNS_FLAG}-{CONST_MAX_GRID_COLUMNS}, 0=auto): ").strip()
                            try:
                                int_cols_choice = int(str_input_cols)
                                if CONST_AUTO_COLUMNS_FLAG <= int_cols_choice <= CONST_MAX_GRID_COLUMNS:
                                    int_current_grid_columns = int_cols_choice
                                    print(f"columns set to {int_current_grid_columns}.")
                                else:
                                    print("invalid choice.")
                            except ValueError:
                                print("bad input.")
                                
                        elif int_setting_choice == 3:
                            break
                        else:
                            print("invalid choice.")

                case EnumMenuAction.EXIT_PROGRAM:
                    print("\nshutting down system...")
                    break

    except Exception as obj_fatal_error:
        print(f"\nFatal Application Error: {obj_fatal_error}")
    except KeyboardInterrupt:
        print("\nInterrupted by user, shutting down...")
    finally:
        # SUBROUTINE: SHUTDOWN DATABASE SERVER AND CLEANUP
        # This block is guaranteed to run, ensuring no ghost processes are left behind.
        util_log_debug("Executing finally block: Shutting down database processes.")
        
        if obj_db_client_process is not None:
            try:
                # We attempt to send a clean SHUTDOWN command to the server via the client pipe.
                obj_db_client_process.stdin.write("SHUTDOWN;\n")
                obj_db_client_process.stdin.flush()
            except Exception:
                pass
            try:
                obj_db_client_process.stdin.close()
            except Exception:
                pass
            try:
                obj_db_client_process.wait(timeout=5)
            except subprocess.TimeoutExpired:
                obj_db_client_process.kill()
                obj_db_client_process.wait()
            try:
                obj_db_client_process.stdout.close()
            except Exception:
                pass

        if obj_db_server_process is not None:
            print("stopping database server...")
            try:
                obj_db_server_process.wait(timeout=CONST_SHUTDOWN_TIMEOUT_SECONDS)
            except subprocess.TimeoutExpired:
                pass
                
            if obj_db_server_process.poll() is None:
                obj_db_server_process.terminate()
                try:
                    obj_db_server_process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass
                    
            if obj_db_server_process.poll() is None:
                if IS_WINDOWS:
                    subprocess.run(["taskkill", "/F", "/T", "/PID", str(obj_db_server_process.pid)], capture_output=True, creationflags=subprocess.CREATE_NO_WINDOW)
                else:
                    os.kill(obj_db_server_process.pid, signal.SIGKILL)
                try:
                    obj_db_server_process.wait(timeout=10)
                except subprocess.TimeoutExpired:
                    pass

        if obj_db_server_log_file is not None:
            obj_db_server_log_file.close()
            
        CONST_SERVER_PID_FILE.unlink(missing_ok=True)
        if path_unix_socket is not None:
            path_unix_socket.unlink(missing_ok=True)

        if IS_WINDOWS and getattr(sys, "frozen", False):
            try:
                input("\npress enter to close...")
            except EOFError:
                pass

if __name__ == "__main__":
    app_main_execution_loop()