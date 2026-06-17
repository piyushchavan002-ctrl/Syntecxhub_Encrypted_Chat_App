"""
====================================================
  ENCRYPTED CHAT SERVER
====================================================
  This server accepts multiple client connections,
  decrypts incoming messages using AES encryption,
  and logs all chat activity to a file.

  HOW IT WORKS:
  1. Server starts and listens for connections
  2. Each client gets its own thread (so many can chat at once)
  3. Every message is AES-encrypted — server decrypts it
  4. Messages are broadcast to all other connected clients
  5. All messages are saved to chat_log.txt

  REQUIRES: pip install cryptography
====================================================
"""

import socket           # For network communication
import threading        # For handling multiple clients at once
import os               # For generating random IVs
import datetime         # For timestamps in logs
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes
from cryptography.hazmat.backends import default_backend
from cryptography.hazmat.primitives import padding

# ─────────────────────────────────────────────
#  CONFIGURATION — Edit these to match client.py
# ─────────────────────────────────────────────

# Pre-shared AES key — MUST be exactly 32 bytes (256-bit AES)
# Both server and client need to use this exact same key!
SECRET_KEY = b'MySecretKey12345MySecretKey12345'   # 32 bytes

# Server network settings
HOST = '127.0.0.1'   # Listen on localhost (change to '0.0.0.0' for LAN)
PORT = 65432          # Port to listen on (must match client)

# Log file where all messages will be saved
LOG_FILE = 'chat_log.txt'

# ─────────────────────────────────────────────
#  GLOBAL STATE
# ─────────────────────────────────────────────

# Dictionary mapping each socket → nickname
# e.g. { <socket>: "Alice", <socket>: "Bob" }
clients = {}

# Lock to prevent race conditions when multiple threads
# read/write the clients dictionary at the same time
clients_lock = threading.Lock()


# ─────────────────────────────────────────────
#  ENCRYPTION HELPERS
# ─────────────────────────────────────────────

def decrypt_message(encrypted_data: bytes) -> str:
    """
    Decrypt a message that was encrypted with AES-CBC.

    The format we expect:  [16 bytes IV] + [encrypted bytes]

    Steps:
    1. Split off the first 16 bytes as the IV
    2. Use AES-CBC with our secret key + that IV to decrypt
    3. Remove PKCS7 padding that was added before encryption
    4. Decode bytes → string
    """
    # The first 16 bytes are the IV (Initialization Vector)
    iv = encrypted_data[:16]

    # Everything after the IV is the actual ciphertext
    ciphertext = encrypted_data[16:]

    # Create the AES cipher in CBC mode
    cipher = Cipher(
        algorithms.AES(SECRET_KEY),
        modes.CBC(iv),
        backend=default_backend()
    )

    # Decrypt the ciphertext
    decryptor = cipher.decryptor()
    padded_plaintext = decryptor.update(ciphertext) + decryptor.finalize()

    # Remove the PKCS7 padding that was added by the client before encrypting
    unpadder = padding.PKCS7(128).unpadder()  # 128-bit block size for AES
    plaintext_bytes = unpadder.update(padded_plaintext) + unpadder.finalize()

    # Convert bytes back to a readable string
    return plaintext_bytes.decode('utf-8')


def encrypt_message(message: str) -> bytes:
    """
    Encrypt a plaintext message using AES-CBC.

    Steps:
    1. Generate a fresh random 16-byte IV for this message
    2. Pad the message to a multiple of 16 bytes (AES block size)
    3. Encrypt using AES-CBC
    4. Return  [IV] + [ciphertext]  so the receiver can decrypt
    """
    # Generate a new random IV for every single message (important for security!)
    iv = os.urandom(16)

    # Add PKCS7 padding so message length is a multiple of AES block size
    padder = padding.PKCS7(128).padder()
    padded_message = padder.update(message.encode('utf-8')) + padder.finalize()

    # Create cipher and encrypt
    cipher = Cipher(
        algorithms.AES(SECRET_KEY),
        modes.CBC(iv),
        backend=default_backend()
    )
    encryptor = cipher.encryptor()
    ciphertext = encryptor.update(padded_message) + encryptor.finalize()

    # Prepend the IV so the client knows how to decrypt
    return iv + ciphertext


# ─────────────────────────────────────────────
#  LOGGING
# ─────────────────────────────────────────────

def log_message(nickname: str, message: str):
    """
    Save a chat message to the log file with a timestamp.
    Format: [2024-01-15 10:30:45] Alice: Hello everyone!
    """
    timestamp = datetime.datetime.now().strftime('%Y-%m-%d %H:%M:%S')
    log_entry = f"[{timestamp}] {nickname}: {message}\n"

    # Open in append mode ('a') so we never overwrite old messages
    with open(LOG_FILE, 'a', encoding='utf-8') as f:
        f.write(log_entry)


# ─────────────────────────────────────────────
#  BROADCASTING
# ─────────────────────────────────────────────

def broadcast(message: str, sender_socket=None):
    """
    Send an encrypted message to every connected client.

    sender_socket: If provided, skip that client (don't echo back to sender).
    """
    encrypted = encrypt_message(message)

    # We need the lock because another thread might be modifying `clients`
    with clients_lock:
        for client_socket in list(clients.keys()):
            # Don't send the message back to the person who sent it
            if client_socket == sender_socket:
                continue
            try:
                # Send a 4-byte length header first, then the encrypted data
                # This tells the receiver exactly how many bytes to read
                length = len(encrypted)
                client_socket.sendall(length.to_bytes(4, byteorder='big') + encrypted)
            except Exception:
                # If sending fails, that client probably disconnected
                # We'll let handle_client() clean it up
                pass


# ─────────────────────────────────────────────
#  CLIENT HANDLER (runs in its own thread)
# ─────────────────────────────────────────────

def handle_client(client_socket: socket.socket, address: tuple):
    """
    This function runs in a separate thread for EACH connected client.
    It handles the full lifecycle: nickname → chat loop → disconnect.
    """
    nickname = None

    try:
        print(f"[+] New connection from {address}")

        # ── Step 1: Ask the client for their nickname ──
        # The first message from any client is always their nickname
        raw = receive_all(client_socket)
        if not raw:
            return
        nickname = decrypt_message(raw).strip()

        # Register this client in our global dictionary
        with clients_lock:
            clients[client_socket] = nickname

        print(f"[+] {nickname} joined from {address}")
        log_message("SERVER", f"{nickname} joined the chat")

        # Let everyone know someone joined
        broadcast(f"** {nickname} has joined the chat! **", sender_socket=client_socket)

        # Send a welcome message just to this client
        welcome = encrypt_message(f"Welcome, {nickname}! You are now connected.")
        length = len(welcome)
        client_socket.sendall(length.to_bytes(4, byteorder='big') + welcome)

        # ── Step 2: Main message loop ──
        while True:
            raw = receive_all(client_socket)
            if not raw:
                # Empty data means the client disconnected
                break

            message = decrypt_message(raw)

            # Check for quit command
            if message.strip().lower() in ('/quit', '/exit', '/q'):
                break

            # Format and display the message on the server console
            formatted = f"{nickname}: {message}"
            print(f"  {formatted}")

            # Save to log file
            log_message(nickname, message)

            # Send to all other clients
            broadcast(formatted, sender_socket=client_socket)

    except ConnectionResetError:
        print(f"[-] {nickname or address} disconnected abruptly")
    except Exception as e:
        print(f"[!] Error with {nickname or address}: {e}")
    finally:
        # ── Step 3: Clean up on disconnect ──
        if nickname:
            print(f"[-] {nickname} left the chat")
            log_message("SERVER", f"{nickname} left the chat")
            broadcast(f"** {nickname} has left the chat. **")

        # Remove from our active clients list
        with clients_lock:
            if client_socket in clients:
                del clients[client_socket]

        client_socket.close()


def receive_all(sock: socket.socket) -> bytes | None:
    """
    Reliably receive a complete message from a socket.

    We use a simple length-prefix protocol:
    - First 4 bytes: the length of the message (as a big-endian integer)
    - Remaining bytes: the actual encrypted message

    This ensures we read exactly the right number of bytes,
    even if the data arrives in multiple TCP packets.
    """
    try:
        # Read the 4-byte length header
        raw_length = recv_exact(sock, 4)
        if not raw_length:
            return None
        message_length = int.from_bytes(raw_length, byteorder='big')

        # Now read exactly that many bytes
        return recv_exact(sock, message_length)
    except Exception:
        return None


def recv_exact(sock: socket.socket, n: int) -> bytes | None:
    """
    Read exactly `n` bytes from a socket.
    TCP can split data across multiple packets, so we keep reading
    until we have all the bytes we need.
    """
    data = b''
    while len(data) < n:
        chunk = sock.recv(n - len(data))
        if not chunk:
            return None   # Connection closed
        data += chunk
    return data


# ─────────────────────────────────────────────
#  MAIN SERVER STARTUP
# ─────────────────────────────────────────────

def start_server():
    """
    Start the chat server:
    1. Create a TCP socket and bind it to HOST:PORT
    2. Listen for incoming connections
    3. For each new client, spin up a dedicated thread
    """
    print("=" * 50)
    print("   🔐 ENCRYPTED CHAT SERVER")
    print("=" * 50)
    print(f"   Host : {HOST}")
    print(f"   Port : {PORT}")
    print(f"   Log  : {LOG_FILE}")
    print("=" * 50)

    # Create the server socket
    # AF_INET = IPv4,  SOCK_STREAM = TCP
    server_socket = socket.socket(socket.AF_INET, socket.SOCK_STREAM)

    # Allow reusing the port immediately after the server restarts
    # (without this you'd get "Address already in use" errors)
    server_socket.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)

    # Bind to our host/port and start listening
    server_socket.bind((HOST, PORT))
    server_socket.listen()

    print(f"\n[*] Listening for connections on {HOST}:{PORT} ...")
    print("[*] Press Ctrl+C to stop the server\n")

    # Write a header line to the log file
    log_message("SERVER", "=== Chat server started ===")

    try:
        while True:
            # accept() blocks here until a client connects
            client_socket, address = server_socket.accept()

            # Spin up a new thread to handle this client
            # daemon=True means the thread dies when the main program exits
            thread = threading.Thread(
                target=handle_client,
                args=(client_socket, address),
                daemon=True
            )
            thread.start()

            # Show how many clients are currently connected
            with clients_lock:
                print(f"[*] Active connections: {len(clients) + 1}")

    except KeyboardInterrupt:
        print("\n\n[*] Server shutting down...")
        log_message("SERVER", "=== Chat server stopped ===")
    finally:
        server_socket.close()
        print("[*] Server socket closed. Goodbye!")


# ─────────────────────────────────────────────
#  ENTRY POINT
# ─────────────────────────────────────────────

if __name__ == '__main__':
    start_server()