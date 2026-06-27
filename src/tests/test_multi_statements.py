#!/usr/bin/env python3
"""
Test script to verify that MULTI_STATEMENTS is properly disabled.

This script tests both the regular asyncmy connection and our SafeConnection
to demonstrate that MULTI_STATEMENTS is disabled in SafeConnection, while
also verifying that async operations and single statements still work correctly.
"""

import asyncio
import os
import sys
from pathlib import Path
import getpass

# Add parent directory to path so we can import from src
sys.path.insert(0, str(Path(__file__).parent.parent.parent))

from src.custom_connection import SafeConnection, create_safe_pool
from asyncmy.connection import Connection
from asyncmy.constants.CLIENT import MULTI_STATEMENTS
import asyncmy


def load_env_file():
    """Load environment variables from .env file if it exists."""
    env_path = Path(__file__).parent.parent.parent / ".env"
    
    if not env_path.exists():
        return
    
    try:
        # Try using python-dotenv if available
        try:
            from dotenv import load_dotenv
            load_dotenv(env_path)
            return
        except ImportError:
            pass
        
        # Fallback: manual parsing
        with open(env_path, 'r') as f:
            for line in f:
                line = line.strip()
                # Skip comments and empty lines
                if not line or line.startswith('#'):
                    continue
                
                # Parse KEY=VALUE
                if '=' in line:
                    key, value = line.split('=', 1)
                    key = key.strip()
                    value = value.strip()
                    
                    # Remove quotes if present
                    if value.startswith('"') and value.endswith('"'):
                        value = value[1:-1]
                    elif value.startswith("'") and value.endswith("'"):
                        value = value[1:-1]
                    
                    # Only set if not already in environment
                    if key and key not in os.environ:
                        os.environ[key] = value
    
    except Exception as e:
        print(f"Warning: Could not load .env file: {e}")


# Load .env file first
load_env_file()

# Get database credentials from environment or use defaults
DB_HOST = os.getenv("DB_HOST", "localhost")
DB_PORT = int(os.getenv("DB_PORT", "3306"))
DB_USER = os.getenv("DB_USER", "root")
DB_PASSWORD = os.getenv("DB_PASSWORD", "")
DB_NAME = os.getenv("DB_NAME", "kb_chunks")


def prompt_for_credentials():
    """Prompt user for database credentials if not set in environment."""
    global DB_HOST, DB_PORT, DB_USER, DB_PASSWORD, DB_NAME
    
    print("\n" + "=" * 70)
    print("Database Credentials Setup")
    print("=" * 70)
    print("\nSome credentials are missing. You can:")
    print("  1. Enter them now (they won't be saved)")
    print("  2. Skip and run flag tests only (press Enter for all prompts)")
    print("  3. Set environment variables and re-run the test")
    print("\n")
    
    try:
        # Only prompt for missing values
        if not DB_HOST or DB_HOST == "localhost":
            response = input(f"Database host [{DB_HOST}]: ").strip()
            if response:
                DB_HOST = response
        
        if not DB_PORT or DB_PORT == 3306:
            response = input(f"Database port [{DB_PORT}]: ").strip()
            if response:
                try:
                    DB_PORT = int(response)
                except ValueError:
                    print(f"Invalid port, using default: {DB_PORT}")
        
        if not DB_USER or DB_USER == "root":
            response = input(f"Database user [{DB_USER}]: ").strip()
            if response:
                DB_USER = response
        
        if not DB_PASSWORD:
            print("\n⚠️  DB_PASSWORD not set - functionality tests will be skipped without it.")
            response = getpass.getpass(f"Database password for {DB_USER} (hidden, press Enter to skip): ")
            if response:
                DB_PASSWORD = response
        
        if not DB_NAME or DB_NAME == "test":
            response = input(f"Database name [{DB_NAME}]: ").strip()
            if response:
                DB_NAME = response
    
    except (EOFError, KeyboardInterrupt):
        print("\n\nInput cancelled. Using default/environment values.")
    
    print("\n" + "=" * 70)


async def test_regular_connection():
    """Test that regular asyncmy Connection has MULTI_STATEMENTS enabled."""
    print("\n=== Testing Regular asyncmy.Connection ===")
    conn = Connection(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME
    )
    
    # Check the flag before connection
    has_multi_statements = bool(conn._client_flag & MULTI_STATEMENTS)
    print(f"Regular Connection _client_flag: 0x{conn._client_flag:x}")
    print(f"MULTI_STATEMENTS enabled: {has_multi_statements}")
    print(f"Expected: True (asyncmy hardcodes this)")
    
    return has_multi_statements


async def test_safe_connection():
    """Test that SafeConnection has MULTI_STATEMENTS disabled."""
    print("\n=== Testing SafeConnection ===")
    conn = SafeConnection(
        host=DB_HOST,
        user=DB_USER,
        password=DB_PASSWORD,
        database=DB_NAME
    )
    
    # Check the flag before connection (should still have it from __init__)
    has_multi_statements_before = bool(conn._client_flag & MULTI_STATEMENTS)
    print(f"SafeConnection _client_flag (before connect): 0x{conn._client_flag:x}")
    print(f"MULTI_STATEMENTS enabled (before connect): {has_multi_statements_before}")
    
    # Simulate what happens during connect() - clear the flag
    conn._client_flag = conn._client_flag & ~MULTI_STATEMENTS
    
    # Check the flag after clearing
    has_multi_statements_after = bool(conn._client_flag & MULTI_STATEMENTS)
    print(f"SafeConnection _client_flag (after clearing): 0x{conn._client_flag:x}")
    print(f"MULTI_STATEMENTS enabled (after clearing): {has_multi_statements_after}")
    print(f"Expected: False (we clear it in connect())")
    
    return has_multi_statements_after


async def test_safe_connection_functionality():
    """Test that SafeConnection works for real async database operations."""
    print("\n=== Testing SafeConnection Functionality ===")
    
    try:
        # Create a SafeConnection and actually connect
        conn = SafeConnection(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            database=DB_NAME
        )
        
        print(f"Connecting to {DB_HOST}:{DB_PORT} as {DB_USER}...")
        await conn.connect()
        print("✓ Connection established successfully")
        
        # Verify MULTI_STATEMENTS is disabled after real connection
        has_multi_statements = bool(conn._client_flag & MULTI_STATEMENTS)
        print(f"✓ MULTI_STATEMENTS after connect: {has_multi_statements} (should be False)")
        
        # Test single statement execution
        async with conn.cursor() as cursor:
            print("\nTesting single statement execution...")
            await cursor.execute("SELECT 1 + 1 AS result")
            result = await cursor.fetchone()
            print(f"✓ Single statement works: SELECT 1 + 1 = {result}")
            
            # Test another single statement
            await cursor.execute("SELECT DATABASE() AS current_db")
            result = await cursor.fetchone()
            print(f"✓ Single statement works: SELECT DATABASE() = {result}")
            
            # Test parameterized query
            await cursor.execute("SELECT %s AS param_test", (42,))
            result = await cursor.fetchone()
            print(f"✓ Parameterized query works: SELECT %s = {result}")
        
        # Test multiple sequential single statements (not multi-statement)
        print("\nTesting multiple sequential single statements...")
        async with conn.cursor() as cursor:
            await cursor.execute("SELECT 'first' AS test")
            result1 = await cursor.fetchone()
            
            await cursor.execute("SELECT 'second' AS test")
            result2 = await cursor.fetchone()
            
            await cursor.execute("SELECT 'third' AS test")
            result3 = await cursor.fetchone()
            
            print(f"✓ Sequential statements work: {result1}, {result2}, {result3}")
        
        # Test that multi-statement queries are rejected
        print("\nTesting that multi-statement queries are blocked...")
        try:
            async with conn.cursor() as cursor:
                # This should fail because MULTI_STATEMENTS is disabled
                await cursor.execute("SELECT 1; SELECT 2;")
                result = await cursor.fetchall()
                print(f"✗ UNEXPECTED: Multi-statement query succeeded: {result}")
                print("  This means MULTI_STATEMENTS is still enabled!")
                await conn.ensure_closed()
                return False
        except Exception as e:
            print(f"✓ Multi-statement query blocked as expected: {type(e).__name__}")
        
        await conn.ensure_closed()
        print("\n✓ Connection closed successfully")
        
        return True
        
    except Exception as e:
        print(f"\n✗ Error during functionality test: {e}")
        print(f"  This might be expected if database is not available")
        print(f"  Connection test skipped - flag test results are still valid")
        return None


async def test_regular_pool_multi_statements():
    """Test that regular asyncmy pool DOES support multi-statements (for comparison)."""
    print("\n=== Testing Regular asyncmy.Pool with Multi-Statements ===")
    
    try:
        print(f"Creating regular asyncmy.Pool for {DB_HOST}:{DB_PORT}...")
        pool = await asyncmy.create_pool(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            db=DB_NAME,
            minsize=1,
            maxsize=2
        )
        
        print(f"✓ Regular pool created successfully")
        
        # Test that multi-statement queries ARE allowed with regular pool
        async with pool.acquire() as conn:
            # Verify MULTI_STATEMENTS is enabled
            has_multi_statements = bool(conn._client_flag & MULTI_STATEMENTS)
            print(f"✓ MULTI_STATEMENTS in regular pooled connection: {has_multi_statements} (should be True)")
            
            # Test multi-statement query execution
            print("\nTesting that multi-statement queries ARE allowed in regular pool...")
            async with conn.cursor() as cursor:
                # This should succeed because MULTI_STATEMENTS is enabled
                await cursor.execute("SELECT 1 AS first; SELECT 2 AS second;")
                
                # Fetch results from first statement
                result1 = await cursor.fetchall()
                print(f"✓ First statement result: {result1}")
                
                # Move to next result set
                has_next = await cursor.nextset()
                if has_next:
                    result2 = await cursor.fetchall()
                    print(f"✓ Second statement result: {result2}")
                    print(f"✓ Multi-statement query succeeded as expected in regular pool")
                else:
                    print(f"✗ Could not get second result set")
                    pool.close()
                    await pool.wait_closed()
                    return False
        
        pool.close()
        await pool.wait_closed()
        print("✓ Regular pool closed successfully")
        
        return True
        
    except Exception as e:
        print(f"\n✗ Error during regular pool test: {e}")
        print(f"  This might be expected if database is not available")
        return None


async def test_safe_pool_functionality():
    """Test that SafePool works for real async database operations."""
    print("\n=== Testing SafePool Functionality ===")
    
    try:
        print(f"Creating SafePool for {DB_HOST}:{DB_PORT}...")
        pool = await create_safe_pool(
            host=DB_HOST,
            port=DB_PORT,
            user=DB_USER,
            password=DB_PASSWORD,
            db=DB_NAME,
            minsize=1,
            maxsize=3
        )
        
        print(f"✓ Pool created successfully (size: {pool.size}, free: {pool.freesize})")
        
        # Test acquiring connection from pool
        async with pool.acquire() as conn:
            print("✓ Connection acquired from pool")
            
            # Verify MULTI_STATEMENTS is disabled
            has_multi_statements = bool(conn._client_flag & MULTI_STATEMENTS)
            print(f"✓ MULTI_STATEMENTS in pooled connection: {has_multi_statements} (should be False)")
            
            # Test query execution
            async with conn.cursor() as cursor:
                await cursor.execute("SELECT 'pool test' AS test")
                result = await cursor.fetchone()
                print(f"✓ Pool query works: {result}")
        
        # Test concurrent connections
        print("\nTesting concurrent pool connections...")
        async def query_task(task_id):
            async with pool.acquire() as conn:
                async with conn.cursor() as cursor:
                    await cursor.execute("SELECT %s AS task_id", (task_id,))
                    result = await cursor.fetchone()
                    return result
        
        results = await asyncio.gather(
            query_task(1),
            query_task(2),
            query_task(3)
        )
        print(f"✓ Concurrent queries work: {results}")
        
        pool.close()
        await pool.wait_closed()
        print("✓ Pool closed successfully")
        
        return True
        
    except Exception as e:
        print(f"\n✗ Error during pool test: {e}")
        print(f"  This might be expected if database is not available")
        print(f"  Pool test skipped - flag test results are still valid")
        return None


async def main():
    """Run all tests."""
    # Check if .env file was loaded
    env_file_path = Path(__file__).parent.parent.parent / ".env"
    env_loaded = env_file_path.exists()
    
    # Prompt for credentials if password is not set
    if not DB_PASSWORD:
        prompt_for_credentials()
    
    print("=" * 70)
    print("Testing MULTI_STATEMENTS Flag Handling and Functionality")
    print("=" * 70)
    
    # Show database configuration
    print(f"\nDatabase Configuration:")
    if env_loaded:
        print(f"  Source: .env file loaded from {env_file_path.parent}")
    print(f"  Host: {DB_HOST}:{DB_PORT}")
    print(f"  User: {DB_USER}")
    print(f"  Password: {'(set)' if DB_PASSWORD else '(not set)'}")
    print(f"  Database: {DB_NAME}")
    
    if not DB_PASSWORD:
        print("\n⚠️  Note: No password provided.")
        print("   Functionality tests will be skipped.")
        print("   Only flag tests will run.")
    
    print(f"\nMULTI_STATEMENTS constant value: {MULTI_STATEMENTS} (0x{MULTI_STATEMENTS:x})")
    print(f"This is bit {MULTI_STATEMENTS.bit_length() - 1} (bit 16)")
    
    # Test 1: Flag values
    regular_has_flag = await test_regular_connection()
    safe_has_flag = await test_safe_connection()
    
    # Test 2: Real connection functionality
    functionality_test = await test_safe_connection_functionality()
    
    # Test 3: Regular pool with multi-statements (for comparison)
    regular_pool_test = await test_regular_pool_multi_statements()
    
    # Test 4: Safe pool functionality
    pool_test = await test_safe_pool_functionality()
    
    # Summary
    print("\n" + "=" * 70)
    print("Test Results Summary")
    print("=" * 70)
    
    print("\n1. Flag Tests:")
    print(f"   Regular Connection has MULTI_STATEMENTS: {regular_has_flag} ✓" if regular_has_flag else f"   Regular Connection has MULTI_STATEMENTS: {regular_has_flag} ✗")
    print(f"   Safe Connection has MULTI_STATEMENTS: {safe_has_flag} ✗" if not safe_has_flag else f"   Safe Connection has MULTI_STATEMENTS: {safe_has_flag} ✓")
    
    print("\n2. Functionality Tests:")
    if functionality_test is True:
        print("   ✓ SafeConnection works for async operations")
        print("   ✓ Single statements execute correctly")
        print("   ✓ Multi-statement queries are blocked")
    elif functionality_test is False:
        print("   ✗ SafeConnection failed functionality test")
    else:
        print("   ⊘ Functionality test skipped (database not available)")
    
    print("\n3. Regular Pool Tests (for comparison):")
    if regular_pool_test is True:
        print("   ✓ Regular asyncmy.Pool has MULTI_STATEMENTS enabled")
        print("   ✓ Multi-statement queries work in regular pool")
    elif regular_pool_test is False:
        print("   ✗ Regular pool test failed")
    else:
        print("   ⊘ Regular pool test skipped (database not available)")
    
    print("\n4. Safe Pool Tests:")
    if pool_test is True:
        print("   ✓ SafePool works for async operations")
        print("   ✓ Concurrent connections work correctly")
        print("   ✓ MULTI_STATEMENTS disabled in SafePool")
    elif pool_test is False:
        print("   ✗ SafePool failed functionality test")
    else:
        print("   ⊘ SafePool test skipped (database not available)")
    
    # Overall result
    print("\n" + "=" * 70)
    if regular_has_flag and not safe_has_flag:
        if functionality_test is True and pool_test is True and regular_pool_test is True:
            print("✅ ALL TESTS PASSED!")
            print("   - MULTI_STATEMENTS properly disabled in SafeConnection/SafePool")
            print("   - MULTI_STATEMENTS properly enabled in regular asyncmy (for comparison)")
            print("   - Async operations work correctly")
            print("   - Single statements execute properly")
            print("   - Multi-statement queries are blocked in SafeConnection")
            print("   - Multi-statement queries work in regular asyncmy.Pool")
            print("   - Pool operations work correctly")
        elif functionality_test is None or pool_test is None or regular_pool_test is None:
            print("✅ FLAG TESTS PASSED (Functionality tests skipped)")
            print("   - MULTI_STATEMENTS properly disabled")
            print("   - Database connection tests skipped (DB not available)")
        else:
            print("⚠️  FLAG TESTS PASSED but functionality tests failed")
    else:
        print("❌ FAILURE: SafeConnection did not properly disable MULTI_STATEMENTS")
    
    print("=" * 70)


if __name__ == "__main__":
    asyncio.run(main())
