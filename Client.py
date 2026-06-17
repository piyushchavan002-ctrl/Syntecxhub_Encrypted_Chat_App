"""
====================================================
  ENCRYPTED CHAT CLIENT
====================================================
  This client connects to the chat server, encrypts
  every message with AES before sending, and decrypts
  messages received from other users.

  HOW IT WORKS:
  1. Connect to the server via TCP
  2. Send your nickname (encrypted) to identify yourself
  3. A background thread listens for incoming messages
  4. Your main thread handles typing & sending messages
  5. Every message is AES-encrypted before it leaves your machine

  REQUIRES: pip install cryptography
  USAGE:    python client.py
====================================================
"""

import socket           # For network communication
import threading        # For receiving messages in the background
import os               # For generating random IVs
import sys              # For clean exit
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding

# ─────────────────────────────────────────────
#  CONFIGURATION — Must match server.py exactly!
# ─────────────────────────────────────────────

# Pre-shared AES key — MUST be exactly 32 bytes (256-bit AES)
# This must be identical to the key in server.py
SECRET_KEY = b'MySecretKey12345MySecretKey12345'   # 32 bytes

# Server address to connect to
SERVER_HOST = '127.0.0.1'   # Server's IP address (localhost for local testing)
SERVER_PORT = 65432           # Must match server.py


# ─────────────────────────────────────────────
#  ENCRYPTION HELPERS
# ─────────────────────────────────────────────

def encrypt_message(message: str) -> bytes:
    """
    Encrypt a plaintext string using AES-CBC encryption.

    Steps:
    1. Generate a fresh random 16-byte IV (never reuse IVs!)
    2. Pad the message so its length is a multiple of 16 (AES block size)
    3. Encrypt with AES using our secret key + the IV
    4. Return  [16-byte IV] + [encrypted bytes]

    Why a random IV?
      Using the same IV twice with the same key leaks information.
      A fresh IV per message keeps each encryption unique and secure.
    """
    # os.urandom gives us cryptographically secure random bytes
    iv = os.urandom(16)

    # PKCS7 padding ensures the plaintext is the right length for AES
    padder = padding.PKCS7(128).padder()  # 128-bit = AES block size
    padded_message = padder.update(message.encode('utf-8')) + padder.finalize()

    # Build the AES cipher in CBC (Cipher Block Chaining) mode
    cipher = Cipher(
        algorithms.AES(SECRET_KEY),
        modes.CBC(iv),
        backend=default_backend()
    )

    # Encrypt!
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(padded_message) + encryptor.finalize()

    # Prepend the IV so the server can decrypt it
    return iv + ciphertext


def decrypt_message(encrypted_data: bytes) -> str:
    """
    Decrypt a message received from the server.

    Format expected:  [16 bytes IV] + [encrypted bytes]

    Steps:
    1. Split off the 16-byte IV from the front
    2. Decrypt the remaining ciphertext using AES-CBC
    3. Strip the PKCS7 padding
    4. Decode bytes → readable string
    """
    iv = encrypted_data[:16]          # First 16 bytes are the IV
    ciphertext = encrypted_data[16:]   # Rest is the encrypted message

    cipher = Cipher(
        algorithms.AES(SECRET_KEY),
        modes.CBC(iv),
        backend=default_backend()
    )

    decryptor = cipher.decryptor()
    padded_plaintext = decryptor.update(ciphertext) + decryptor.finalize()

    # Remove padding to get the original message back
    unpadder = padding.PKCS7(128).unpadder()
    plaintext_bytes = unpadder.update(padded_plaintext) + unpadder.finalize()

    return plaintext_bytes.decode('utf-8')


# ─────────────────────────────────────────────
#  SOCKET HELPERS (Length-Prefix Protocol)
# ─────────────────────────────────────────────

def send_message(sock: socket.socket, message: str):
    """
    Encrypt a message and send it over the socket.

    We prefix every message with its 4-byte length so the
    receiver knows exactly how many bytes to read.

    Wire format:  [4 bytes: length][N bytes: encrypted message]
    """
    encrypted = encrypt_message(message)
    length = len(encrypted)

    # Pack the length as a 4-byte big-endian integer, then send data
    sock.sendall(length.to_bytes(4, byteorder='big') + encrypted)


def receive_all(sock: socket.socket) -> bytes | None:
    """
    Receive a complete message from the socket.
    Reads the 4-byte length header first, then reads exactly that many bytes.
    Returns None if the connection was closed.
    """
    try:
        raw_length = recv_exact(sock, 4)
        if not raw_length:
            return None
        message_length = int.from_bytes(raw_length, byteorder='big')
        return recv_exact(sock, message_length)
    except Exception:
        return None


def recv_exact(sock: socket.socket, n: int) -> bytes | None:
    """
    Read exactly `n` bytes from the socket.
    Keeps reading in a loop until all bytes arrive (TCP can split data).
    """
    data = b''
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            return None
        data += chunk
    return data


# ─────────────────────────────────────────────
#  RECEIVE THREAD
# ─────────────────────────────────────────────

# A flag shared between threads — set to True to signal shutdown
stop_event = threading.Event()


def receive_messages(sock: socket.socket):
    """
    This function runs in a BACKGROUND THREAD.
    It continuously listens for incoming messages from the server,
    decrypts them, and prints them to the console.

    Running this in a separate thread means you can type and send
    messages at the same time without the two interfering.
    """
    while not stop_event.is_set():
        try:
            raw = receive_all(sock)
            if not raw:
                # Server closed the connection
                print("\n[!] Disconnected from server.")
                stop_event.set()
                break

            message = decrypt_message(raw)

            # Print the message, then reprint the "> " prompt
            # so the UI stays tidy even when messages arrive mid-typing
            print(f"\n  📨 {message}")
            print("  > ", end='', flush=True)

        except Exception:
            if not stop_event.is_set():
                print("\n[!] Connection error while receiving.")
                stop_event.set()
            break


# ─────────────────────────────────────────────
#  MAIN CLIENT LOGIC
# ─────────────────────────────────────────────

def start_client():
    """
    Main function that:
    1. Connects to the server
    2. Sends the user's nickname
    3. Starts the background receive thread
    4. Runs the interactive send loop
    """

    # ── Welcome Banner ──
    print("=" * 50)
    print("   🔐 ENCRYPTED CHAT CLIENT")
    print("=" * 50)
    print(f"   Server : {SERVER_HOST}:{SERVER_PORT}")
    print("=" * 50)

    # ── Get Nickname ──
    print()
    nickname = input("  Enter your nickname: ").strip()
    if not nickname:
        nickname = "Anonymous"

    # ── Connect to Server ──
    print(f"\n[*] Connecting to {SERVER_HOST}:{SERVER_PORT} ...")

    try:
        # Create a TCP socket and connect to the server
        sock = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
        sock.connect((SERVER_HOST, SERVER_PORT))
        print("[+] Connected!\n")
    except ConnectionRefusedError:
        print(f"[!] Could not connect to {SERVER_HOST}:{SERVER_PORT}")
        print("[!] Make sure the server is running first.")
        sys.exit(1)
    except Exception as e:
        print(f"[!] Connection error: {e}")
        sys.exit(1)

    # ── Send Nickname to Server ──
    # The server expects the first message to be the nickname
    try:
        send_message(sock, nickname)
    except Exception as e:
        print(f"[!] Failed to send nickname: {e}")
        sock.close()
        sys.exit(1)

    # ── Start Background Receive Thread ──
    # This thread silently listens and prints incoming messages
    recv_thread = threading.Thread(
        target=receive_messages,
        args=(sock,),
        daemon=True   # Dies automatically when the main thread exits
    )
    recv_thread.start()

    # ── Print Usage Help ──
    print("─" * 50)
    print("  💬 Chat is ready! Type a message and press Enter.")
    print("  Type /quit  to leave the chat.")
    print("─" * 50)
    print()

    # ── Main Send Loop ──
    try:
        while not stop_event.is_set():
            print("  > ", end='', flush=True)

            # Read a line of input from the user
            try:
                message = input()
            except EOFError:
                # Happens if stdin is closed (e.g. piped input ends)
                break

            # Skip empty messages (user just pressed Enter)
            if not message.strip():
                continue

            # Handle quit commands
            if message.strip().lower() in ('/quit', '/exit', '/q'):
                print("\n[*] Leaving the chat. Goodbye!")
                send_message(sock, '/quit')   # Politely notify the server
                break

            # ── Encrypt & Send ──
            try:
                send_message(sock, message)
            except Exception as e:
                print(f"[!] Failed to send message: {e}")
                break

    except KeyboardInterrupt:
        print("\n\n[*] Interrupted. Disconnecting...")

    finally:
        # ── Clean Shutdown ──
        stop_event.set()
        try:
            sock.close()
        except Exception:
            pass
        print("[*] Connection closed.")


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == '__main__':
    start_client()
