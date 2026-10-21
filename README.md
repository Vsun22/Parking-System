# Parking-System
A Database Management System (DBMS) based Smart Parking Management System built to handle vehicle entry/exit, slot tracking, owner record management, and parking fee calculations with a visual grid layout.
mart Parking Management System

A parking management system in Python with Mysql/MariaDB A python and mysql/mariadb based application that manages vehicle entry and check out, parking charges, parking slots and daily revenue along with colored ascii parking maps. In application code, it launches an embedded database server process, then keeps its data alongside the code.

The application requires MySQL (or MariaDB) to be already installed, the database engine itself is not included.

Features
- Park a car in the slot that you chose then plit your ticket.
- See vehicles and find out how much it will cost.
- Calculate the parking charge currently in force prior to checkout.
- See occupied and free slots with single dot.
- By hand or fit to the terminal width.
- Track completed stays and daily revenue totals using database triggers.
- Backdate a single ticket for demonstration and fee testing.
- Automatically initialize the database on first launch and persist data between runs.

Requirements

- Python 3.10+ is needed to run this code - match/case syntax.
- Windows or Linux.
- For the Server, the Command Line Client, etc.
- The only third-party python dependency used by the script. Colorama.
- Write access to the directory where the application is located.
  
Required database executables must be discoverable through PATH:
Database Server Client Initialization
MySQL mysqld mysql Server's --initialize-insecure option
MariaDB sudo mariadbd or mysqld mariadb or mysql mariadb-install-db or mysqlinstalldb

On Linux, the application looks in /usr/sbin, /usr/local/sbin, /usr/libexec, and /sbin. The ability to run with a specific database version and the flags supported by the database installation determine whether the application can run successfully.

Setup and Run

1. Clone this repo and open a terminal in its directory.
2. Install MySQL or MariaDB and have the above executables available.
3. Save your application code main.py or put in the actual filename of your script in the commands below.
4. Install the Python dependency:
python -m pip install colorama
5. Start the application:
python main.py

If using a system with python3 specified as python3, use that in both commands.
On first run, it generates a private data directory, creates database accounts, launches the database on a local TCP port assigned randomly, then makes the tables, views, functions, triggers and stored procedures. On subsequent runs, it re-uses the stored database and simply recreates the routines and views for the app.

Menu

Option Action

1 Park a vehicle
2 Check out a vehicle
3 Estimate parking cost
4 Fast forward time by backdating a ticket
5 Show parking grid, live tickets, and revenue for the day
6 Change grid style or column number
7 Log off and bring down the private database server

Example workflow
1. Enter 1 and input a plate i.e. MH12AB1234.
2. Select a vehicle type and an available time slot.
3. Check 3 to see how the current estimated fee.
4. By default, pick 4 and backdate that ticket by 1-720 hours for a test.
5. Select 2 to view and note the final bill.
6. Choose 5 to see current occupancy and revenue.

The end allows don the plates 4-15 letters or digital, which it in majuscules. Two database routines specifically allow normalized don the plates 4-20 characters, that eliminate spaces, dashes and dots.
Parking Rates
Vehicle Type Rate/hr
2-Wheeler 20.00
4-Wheeler 50.00
Heavy 100.00

Rates must be numerical values. No currency is applied by this app.

- Free if less than 15 minutes.
- Longer stays are rounded up to whole hours, from check-in time.
- For instance, 4-Wheeler– 16 minutes hold charged 50.00; 61 minutes hold charged 100.00.

Default rates are inserted to cover the null values. When the app is restarted, the rates are preserved, and not reset on Vehicle_Categories.

Database Design
Table Purpose

Vehicle_Categories Vehicle types and hourly rates
Vehicles Registered plates and their categories
Active_Parking Active Parking, available tickets, occupied slots, and entry times
Parking_History Completed stays and final fees
Revenue_Audit Daily collection totals maintained by triggers
Views: ActiveParkingView contains human-readable active-ticket data; ActiveParkingFee_View contains live fee estimates.
Operations: NormalizePlate verifies the license plate; CalculateParking_Fee calculates the fee.
Transactions: spParkVehicle, and spCheckoutVehicle, perform transactions for adding entries and performing checkouts. SpBackdateTicket modifies the entry time of one active ticket for testing purposes.
Unique constraints on duplicate active plates, and full slots. Foreign keys on vehicles, categories and parking records. History triggers after on insert, update and delete to update daily revenue.

