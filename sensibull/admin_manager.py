#!/usr/bin/env python3
"""
Admin User Management Script for Sensibull Position Tracker

Usage:
    python3 admin_manager.py add <username> <password> [email]
    python3 admin_manager.py delete <username>
    python3 admin_manager.py list
    python3 admin_manager.py view <username>
    python3 admin_manager.py verify <username> <password>
    python3 admin_manager.py change-password <username> <new_password>
"""

import sys
import sqlite3
from werkzeug.security import generate_password_hash
from database import get_db, now_ist

def add_admin(username, password, email=None):
    """Add a new admin user"""
    if not username or not password:
        print("Error: Username and password are required")
        return False
    
    if len(password) < 6:
        print("Error: Password must be at least 6 characters long")
        return False
    
    conn = get_db()
    c = conn.cursor()
    
    try:
        password_hash = generate_password_hash(password, method='pbkdf2:sha256')
        c.execute("""
            INSERT INTO admin_users (username, password_hash, email, created_at)
            VALUES (?, ?, ?, ?)
        """, (username, password_hash, email, now_ist().isoformat()))
        
        conn.commit()
        print(f"✓ Admin user '{username}' created successfully")
        return True
    except sqlite3.IntegrityError:
        print(f"✗ Error: Username '{username}' already exists")
        return False
    except Exception as e:
        print(f"✗ Error creating admin user: {e}")
        return False
    finally:
        conn.close()

def delete_admin(username):
    """Delete an admin user"""
    if not username:
        print("Error: Username is required")
        return False
    
    conn = get_db()
    c = conn.cursor()
    
    try:
        c.execute("DELETE FROM admin_users WHERE username = ?", (username,))
        if c.rowcount > 0:
            conn.commit()
            print(f"✓ Admin user '{username}' deleted successfully")
            return True
        else:
            print(f"✗ Error: Admin user '{username}' not found")
            return False
    except Exception as e:
        print(f"✗ Error deleting admin user: {e}")
        return False
    finally:
        conn.close()

def list_admins():
    """List all admin users"""
    conn = get_db()
    c = conn.cursor()
    
    try:
        admins = c.execute("""
            SELECT username, email, is_active, created_at, last_login
            FROM admin_users
            ORDER BY created_at DESC
        """).fetchall()
        
        if not admins:
            print("No admin users found")
            return
        
        print("\n" + "=" * 80)
        print(f"{'Username':<20} {'Email':<30} {'Active':<8} {'Created':<20}")
        print("=" * 80)
        
        for admin in admins:
            status = "Yes" if admin['is_active'] else "No"
            email = admin['email'] or "-"
            created = admin['created_at'][:19] if admin['created_at'] else "-"
            print(f"{admin['username']:<20} {email:<30} {status:<8} {created:<20}")
        
        print("=" * 80 + "\n")
        
    except Exception as e:
        print(f"✗ Error listing admin users: {e}")
    finally:
        conn.close()

def view_admin(username):
    """View detailed information about an admin user"""
    if not username:
        print("Error: Username is required")
        return False
    
    conn = get_db()
    c = conn.cursor()
    
    try:
        admin = c.execute("""
            SELECT id, username, email, is_active, created_at, last_login
            FROM admin_users
            WHERE username = ?
        """, (username,)).fetchone()
        
        if not admin:
            print(f"✗ Error: Admin user '{username}' not found")
            return False
        
        print("\n" + "=" * 60)
        print(f"  Admin User Details: {username}")
        print("=" * 60)
        print(f"  ID:           {admin['id']}")
        print(f"  Username:     {admin['username']}")
        print(f"  Email:        {admin['email'] or '(not set)'}")
        print(f"  Status:       {'Active' if admin['is_active'] else 'Inactive'}")
        print(f"  Created:      {admin['created_at'] or '(unknown)'}")
        print(f"  Last Login:   {admin['last_login'] or '(never)'}")
        print("=" * 60)
        print("\n  Note: Passwords are securely hashed and cannot be retrieved.")
        print("  Use 'verify' command to test a password, or 'change-password' to reset it.\n")
        
        return True
        
    except Exception as e:
        print(f"✗ Error viewing admin user: {e}")
        return False
    finally:
        conn.close()

def verify_password(username, password):
    """Verify if a password is correct for a user"""
    if not username or not password:
        print("Error: Username and password are required")
        return False
    
    conn = get_db()
    c = conn.cursor()
    
    try:
        admin = c.execute("""
            SELECT username, password_hash, is_active
            FROM admin_users
            WHERE username = ?
        """, (username,)).fetchone()
        
        if not admin:
            print(f"✗ Error: Admin user '{username}' not found")
            return False
        
        from werkzeug.security import check_password_hash
        
        if check_password_hash(admin['password_hash'], password):
            print(f"✓ Password is CORRECT for user '{username}'")
            if not admin['is_active']:
                print(f"  ⚠ Warning: User is currently INACTIVE")
            return True
        else:
            print(f"✗ Password is INCORRECT for user '{username}'")
            return False
        
    except Exception as e:
        print(f"✗ Error verifying password: {e}")
        return False
    finally:
        conn.close()

def change_password(username, new_password):
    """Change password for an admin user"""
    if not username or not new_password:
        print("Error: Username and new password are required")
        return False
    
    if len(new_password) < 6:
        print("Error: Password must be at least 6 characters long")
        return False
    
    conn = get_db()
    c = conn.cursor()
    
    try:
        password_hash = generate_password_hash(new_password, method='pbkdf2:sha256')
        c.execute("""
            UPDATE admin_users 
            SET password_hash = ?
            WHERE username = ?
        """, (password_hash, username))
        
        if c.rowcount > 0:
            conn.commit()
            print(f"✓ Password changed successfully for user '{username}'")
            return True
        else:
            print(f"✗ Error: Admin user '{username}' not found")
            return False
    except Exception as e:
        print(f"✗ Error changing password: {e}")
        return False
    finally:
        conn.close()

def print_usage():
    """Print usage instructions"""
    print(__doc__)

def main():
    if len(sys.argv) < 2:
        print_usage()
        sys.exit(1)
    
    command = sys.argv[1].lower()
    
    if command == 'add':
        if len(sys.argv) < 4:
            print("Usage: python3 admin_manager.py add <username> <password> [email]")
            sys.exit(1)
        username = sys.argv[2]
        password = sys.argv[3]
        email = sys.argv[4] if len(sys.argv) > 4 else None
        add_admin(username, password, email)
    
    elif command == 'delete':
        if len(sys.argv) < 3:
            print("Usage: python3 admin_manager.py delete <username>")
            sys.exit(1)
        username = sys.argv[2]
        delete_admin(username)
    
    elif command == 'list':
        list_admins()
    
    elif command == 'view':
        if len(sys.argv) < 3:
            print("Usage: python3 admin_manager.py view <username>")
            sys.exit(1)
        username = sys.argv[2]
        view_admin(username)
    
    elif command == 'verify':
        if len(sys.argv) < 4:
            print("Usage: python3 admin_manager.py verify <username> <password>")
            sys.exit(1)
        username = sys.argv[2]
        password = sys.argv[3]
        verify_password(username, password)
    
    elif command == 'change-password':
        if len(sys.argv) < 4:
            print("Usage: python3 admin_manager.py change-password <username> <new_password>")
            sys.exit(1)
        username = sys.argv[2]
        new_password = sys.argv[3]
        change_password(username, new_password)
    
    else:
        print(f"Unknown command: {command}")
        print_usage()
        sys.exit(1)

if __name__ == '__main__':
    main()
