-- Fixture schema for the MCP server integration tests.
--
-- Deliberately exercises the DL-5840 edge cases:
--   * tables and columns WITH comments, and WITHOUT them
--   * a column literally named `comment` (namespace collision)
--   * a VIEW, for which MariaDB reports TABLE_COMMENT as the string 'VIEW'
--   * foreign keys, so comments and FK metadata are asserted together
--   * a second database, to verify comment lookups are scoped per-database
--   * a column DEFAULT, which DESCRIBE and INFORMATION_SCHEMA report differently

CREATE DATABASE IF NOT EXISTS mcp_test CHARACTER SET utf8mb4;
CREATE DATABASE IF NOT EXISTS mcp_test_other CHARACTER SET utf8mb4;

USE mcp_test;

CREATE TABLE parents (
    id   INT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT 'Primary key.',
    name VARCHAR(100) NOT NULL COMMENT 'Display name of the parent record.',
    PRIMARY KEY (id)
) COMMENT='Parent records referenced by documented.parent_id.';

CREATE TABLE documented (
    id              INT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT 'Primary key.',
    parent_id       INT UNSIGNED NULL COMMENT 'FK -> parents.id; the owning parent record.',
    email           VARCHAR(255) NOT NULL COMMENT 'Login address; unique per parent.',
    status          VARCHAR(20)  NOT NULL DEFAULT 'active' COMMENT 'Lifecycle state: active, paused or closed.',
    uncommented_col INT NULL,
    notes           TEXT NULL COMMENT 'Free-form notes; may contain "quotes", commas and ünicode ✓.',
    PRIMARY KEY (id),
    UNIQUE KEY uq_documented_email (email),
    KEY idx_documented_parent (parent_id),
    CONSTRAINT fk_documented_parent FOREIGN KEY (parent_id)
        REFERENCES parents (id) ON DELETE SET NULL ON UPDATE CASCADE
) COMMENT='Fully documented table used to verify comment passthrough.';

-- No table comment and no column comments: everything must come back as ''.
CREATE TABLE undocumented (
    id    INT UNSIGNED NOT NULL AUTO_INCREMENT,
    value VARCHAR(50) NULL,
    PRIMARY KEY (id)
);

-- The table-level comment must not be shadowed by this column's name.
CREATE TABLE with_comment_column (
    id      INT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT 'Primary key.',
    comment VARCHAR(255) NULL COMMENT 'A column literally named comment.',
    PRIMARY KEY (id)
) COMMENT='Table with a column named comment, to catch namespace collisions.';

-- MariaDB does not support a COMMENT clause on views and always reports
-- TABLE_COMMENT='VIEW' for them, so this must surface as an empty comment.
CREATE VIEW documented_view AS
    SELECT id, email, status FROM documented;

-- Every shape of column default, because INFORMATION_SCHEMA.COLUMN_DEFAULT
-- reports them as SQL literals ("'active'", and the string "NULL" for no
-- default) and the server has to decode them back to plain values.
CREATE TABLE column_defaults (
    s_str       VARCHAR(20) NOT NULL DEFAULT 'active'  COMMENT 'Quoted string default.',
    s_empty     VARCHAR(20) NOT NULL DEFAULT ''        COMMENT 'Empty string default.',
    s_quote     VARCHAR(20) NOT NULL DEFAULT 'it''s'   COMMENT 'Default containing a single quote.',
    s_backslash VARCHAR(20) NOT NULL DEFAULT 'a\\b'    COMMENT 'Default containing a backslash.',
    s_null_word VARCHAR(20) NOT NULL DEFAULT 'NULL'    COMMENT 'Literal string NULL, not an absent default.',
    n_int       INT NOT NULL DEFAULT 5                 COMMENT 'Integer default.',
    n_dec       DECIMAL(5,2) NOT NULL DEFAULT 1.50     COMMENT 'Decimal default.',
    e_enum      ENUM('a','b') NOT NULL DEFAULT 'b'     COMMENT 'Enum default.',
    d_none      VARCHAR(20) NULL                       COMMENT 'No default at all.',
    d_expnull   VARCHAR(20) NULL DEFAULT NULL          COMMENT 'Explicit DEFAULT NULL.',
    ts_now      TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP COMMENT 'Expression default.',
    -- DESCRIBE renders a TEXT default as a quoted SQL expression ("'[]'"),
    -- which misreports the actual two-character value. INFORMATION_SCHEMA plus
    -- normalization gets this right.
    t_json      MEDIUMTEXT NOT NULL DEFAULT '[]'       COMMENT 'TEXT column default.'
) COMMENT='Covers every column-default shape for normalization tests.';

USE mcp_test_other;

CREATE TABLE other_documented (
    id    INT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT 'Primary key in the second database.',
    label VARCHAR(50) NULL COMMENT 'Label column in the second database.',
    PRIMARY KEY (id)
) COMMENT='Table in a second database, to verify cross-database comment lookups.';

-- Same table and column names as mcp_test.documented, but different comments,
-- so a lookup that ignores TABLE_SCHEMA would return the wrong text.
CREATE TABLE documented (
    id    INT UNSIGNED NOT NULL AUTO_INCREMENT COMMENT 'Second-database id, NOT the mcp_test one.',
    email VARCHAR(255) NULL COMMENT 'Second-database email, NOT the mcp_test one.',
    PRIMARY KEY (id)
) COMMENT='Decoy table sharing a name with mcp_test.documented.';

-- Broad privileges: this is a disposable container, and the tests need to
-- create databases (create_database) and read information_schema fully.
GRANT ALL PRIVILEGES ON *.* TO 'mcp_test'@'%' WITH GRANT OPTION;
FLUSH PRIVILEGES;
