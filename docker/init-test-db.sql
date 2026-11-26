-- Runs once, when the MySQL volume is created: the database the test suite uses.
CREATE DATABASE IF NOT EXISTS capium_test CHARACTER SET utf8mb4 COLLATE utf8mb4_0900_as_cs;
GRANT ALL PRIVILEGES ON capium_test.* TO 'capium'@'%';
