import tkinter as tk
from tkinter import filedialog, messagebox
import zipfile
import os

def select_file():
    """ZIP-Datei auswählen"""
    file = filedialog.askopenfilename(filetypes=[("ZIP Dateien", "*.zip")])
    if file:
        entry_path.delete(0, tk.END)
        entry_path.insert(0, file)

def analyze_zip():
    """Analysiert die ausgewählte ZIP-Datei"""
    zip_path = entry_path.get()
    if not zip_path or not os.path.isfile(zip_path):
        messagebox.showerror("Fehler", "Bitte wähle eine gültige ZIP-Datei aus.")
        return

    try:
        # 1. Größe der ZIP-Datei auf der Festplatte
        disk_size = os.path.getsize(zip_path)

        # 2. ZIP-Datei öffnen und Inhalte auslesen
        with zipfile.ZipFile(zip_path, 'r') as zf:
            info_list = zf.infolist()
            num_files = len(info_list)

            total_compressed = 0   # komprimierte Größe der Daten (ohne Overhead)
            total_uncompressed = 0 # entpackte Größe (das ist der "Inhalt")

            for info in info_list:
                total_compressed += info.compress_size
                total_uncompressed += info.file_size

        # Ergebnisse formatieren
        disk_size_str = format_size(disk_size)
        comp_size_str = format_size(total_compressed)
        unzip_size_str = format_size(total_uncompressed)

        # Ausgabe aktualisieren
        lbl_disk.config(text=f"📦 Größe der ZIP-Datei: {disk_size_str}")
        lbl_comp.config(text=f"📊 Komprimierte Daten (netto): {comp_size_str}")
        lbl_unzip.config(text=f"📂 Entpackter Inhalt (benötigter Platz): {unzip_size_str}")
        lbl_files.config(text=f"📄 Anzahl enthaltene Dateien/Ordner: {num_files}")

        lbl_status.config(text="Fertig", fg="green")
    except zipfile.BadZipFile:
        messagebox.showerror("Fehler", "Die Datei ist keine gültige ZIP-Datei oder ist beschädigt.")
        lbl_status.config(text="Fehler", fg="red")
    except Exception as e:
        messagebox.showerror("Fehler", f"Ein unerwarteter Fehler ist aufgetreten:\n{e}")
        lbl_status.config(text="Fehler", fg="red")

def format_size(bytes):
    """Bytes in lesbare Einheiten umwandeln"""
    for unit in ['B', 'KB', 'MB', 'GB', 'TB']:
        if bytes < 1024.0:
            return f"{bytes:.2f} {unit}"
        bytes /= 1024.0
    return f"{bytes:.2f} PB"

# ---------- GUI erstellen ----------
root = tk.Tk()
root.title("ZIP-Inhaltsgröße analysieren")
root.geometry("600x260")
root.resizable(False, False)

# Pfadauswahl
tk.Label(root, text="ZIP-Datei:").grid(row=0, column=0, padx=5, pady=15, sticky="w")
entry_path = tk.Entry(root, width=45)
entry_path.grid(row=0, column=1, padx=5, pady=15)
btn_browse = tk.Button(root, text="Durchsuchen...", command=select_file)
btn_browse.grid(row=0, column=2, padx=5, pady=15)

# Button zum Analysieren
btn_analyze = tk.Button(root, text="Inhalt analysieren", command=analyze_zip, bg="lightgreen")
btn_analyze.grid(row=1, column=1, pady=10)

# Ergebnisse anzeigen (Labels)
lbl_files = tk.Label(root, text="", font=("Arial", 10))
lbl_files.grid(row=2, column=0, columnspan=3, pady=2, sticky="w", padx=20)

lbl_disk = tk.Label(root, text="", font=("Arial", 10))
lbl_disk.grid(row=3, column=0, columnspan=3, pady=2, sticky="w", padx=20)

lbl_comp = tk.Label(root, text="", font=("Arial", 10))
lbl_comp.grid(row=4, column=0, columnspan=3, pady=2, sticky="w", padx=20)

lbl_unzip = tk.Label(root, text="", font=("Arial", 10, "bold"))
lbl_unzip.grid(row=5, column=0, columnspan=3, pady=2, sticky="w", padx=20)

# Statuszeile
lbl_status = tk.Label(root, text="Bereit", fg="gray")
lbl_status.grid(row=6, column=0, columnspan=3, pady=15)

root.mainloop()