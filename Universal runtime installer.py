import os
import sys
import ctypes
import locale
import subprocess
import threading
import queue
import traceback
import json
import logging
from pathlib import Path
import ttkbootstrap as ttk
from tkinter import messagebox, filedialog
from ttkbootstrap.widgets.scrolled import ScrolledText

class GuiLogHandler(logging.Handler):
    """Custom logging handler to send logs to the GUI queue."""
    def __init__(self, msg_queue):
        super().__init__()
        self.queue = msg_queue

    def emit(self, record):
        log_entry = self.format(record)
        self.queue.put({
            "type": "log",
            "message": log_entry,
            "level": record.levelname
        })

def resource_path(relative_path):
    """Get absolute path to resource, works for development and for PyInstaller packaging."""
    try:
        base_path = Path(sys._MEIPASS)
    except AttributeError:
        base_path = Path(".").resolve()
    return base_path / relative_path

class InstallationManager:
    def __init__(self, msg_queue, lang):
        self.queue = msg_queue
        self.lang = lang
        self.cancelled = False
        self.current_process = None
        self.logger = logging.getLogger("Installer")

    def run_command(self, command):
        """Execute a command list and return output, error, and exit code."""
        process = None
        try:
            # shell=False is safer and preferred for lists
            process = subprocess.Popen(
                command,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                encoding='utf-8',
                errors='replace',
                creationflags=subprocess.CREATE_NO_WINDOW if os.name == 'nt' else 0
            )
            self.current_process = process

            while True:
                if self.cancelled:
                    process.terminate()
                    try:
                        process.wait(timeout=2)
                    except subprocess.TimeoutExpired:
                        process.kill()
                        process.wait()
                    return "", self.lang["installation_cancelled"], -2

                try:
                    output, error = process.communicate(timeout=0.2)
                    break
                except subprocess.TimeoutExpired:
                    continue
            
            # Clean output from progress bar characters if it's a winget command
            if command[0] == "winget":
                output = self._clean_winget_output(output)
                error = self._clean_winget_output(error)

            return output, error, process.returncode
        except Exception as e:
            return "", str(e), -1
        finally:
            if self.current_process is process:
                self.current_process = None

    def _clean_winget_output(self, text):
        """Remove progress bar and other messy characters from winget output."""
        if not text:
            return ""
        # Remove backspaces and following/preceding characters often used in progress bars
        import re
        # This matches common progress bar patterns and backspace characters
        text = re.sub(r'[\b\r]', '', text)
        text = re.sub(r'[░▒▓█]', '', text)
        # Remove multiple spaces
        text = re.sub(r' +', ' ', text)
        return text.strip()

    def check_winget(self):
        """Check if winget is available."""
        version_out, version_err, version_code = self.run_command(["winget", "--version"])
        if version_code != 0:
            self.logger.error(self.lang["winget_not_installed_log"])
            return False

        version = version_out.strip() or version_err.strip()
        self.logger.info(self.lang["winget_version"].format(version=version))
        self.queue.put({"type": "status", "message": self.lang["checking_winget_updates"]})

        precheck_commands = [
            ["winget", "source", "list"],
            ["winget", "source", "update"]
        ]

        for cmd in precheck_commands:
            output, error, code = self.run_command(cmd)
            if code != 0:
                err_msg = error.strip() or output.strip()
                self.logger.error(self.lang["pre_install_cmd_fail"].format(errmsg=err_msg))
                return False

            cmd_output = output.strip()
            if cmd_output:
                self.logger.info(self.lang["pre_install_check_succeeded"].format(msg=cmd_output))

        self.logger.info(self.lang["winget_up_to_date"])
        return True

    def install_programs(self, selected_programs):
        """Main installation loop."""
        total_commands = sum(len(p['commands']) for p in selected_programs)
        completed_commands = 0
        failed_programs = []

        for program in selected_programs:
            if self.cancelled:
                break

            self.queue.put({"type": "status", "message": self.lang["installing"].format(prog=program['name'])})
            self.logger.info(self.lang["installing"].format(prog=program['name']))
            
            program_failed = False
            for cmd in program['commands']:
                if self.cancelled:
                    break

                output, error, code = self.run_command(cmd)
                if self.cancelled:
                    break

                completed_commands += 1
                
                # Update total progress based on commands
                total_progress = (completed_commands / total_commands) * 100
                self.queue.put({"type": "progress_total", "value": total_progress})

                if code != 0:
                    err_msg = error.strip() or output.strip()
                    # Check if already installed
                    if "already installed" in err_msg.lower() or "bereits installiert" in err_msg.lower():
                        self.logger.info(self.lang["already_installed"].format(prog=program['name']))
                    else:
                        self.logger.warning(self.lang["error_installing"].format(prog=program["name"], error=err_msg))
                        
                        # Try upgrade if it was an install command
                        if len(cmd) > 1 and cmd[1] == "install":
                            upgrade_cmd = list(cmd)
                            upgrade_cmd[1] = "upgrade"
                            # Ensure --silent and --disable-interactivity are present if it's a winget command
                            if upgrade_cmd[0] == "winget":
                                if "--silent" not in upgrade_cmd:
                                    upgrade_cmd.append("--silent")
                                if "--disable-interactivity" not in upgrade_cmd:
                                    upgrade_cmd.append("--disable-interactivity")
                            up_out, up_err, up_code = self.run_command(upgrade_cmd)
                            if up_code == 0:
                                self.logger.info(self.lang["upgraded_successfully"].format(prog=program["name"]))
                            else:
                                up_err_msg = up_err.strip() or up_out.strip()
                                self.logger.error(self.lang["error_upgrading"].format(prog=program["name"], error=up_err_msg))
                                program_failed = True
                        else:
                            program_failed = True
                else:
                    self.logger.info(self.lang["success"] + ": " + program['name'])
                    if output.strip():
                        self.logger.debug(f"Output: {output.strip()}")

            if program_failed:
                failed_programs.append(program["name"])

        if not self.cancelled:
            if failed_programs:
                failed_str = "\n".join(failed_programs)
                self.queue.put({"type": "finish", "success": False, "failed": failed_str})
            else:
                self.queue.put({"type": "finish", "success": True})
        else:
            self.queue.put({"type": "status", "message": self.lang["installation_cancelled"]})
            self.logger.warning(self.lang["installation_cancelled"])

class InstallerGUI:
    def __init__(self, root):
        self.root = root
        self.queue = queue.Queue()
        self.cancelled = False
        
        self.load_language()
        self.load_programs()
        
        self.manager = InstallationManager(self.queue, self.lang)
        self.vars = [ttk.BooleanVar(value=True) for _ in self.programs]
        self.select_all_var = ttk.BooleanVar(value=True)
        self.vc_select_all_var = ttk.BooleanVar(value=True)
        
        self.setup_ui()
        self.setup_logging()
        self.queue_after_id = None
        self.is_closing = False
        self.root.protocol("WM_DELETE_WINDOW", self.on_close)

    def load_language(self):
        """Determine language and load from JSON."""
        try:
            # locale.getdefaultlocale() is deprecated, but for compatibility:
            lang_code, _ = locale.getdefaultlocale()
        except Exception:
            lang_code = "en"
            
        lang_to_load = "de" if lang_code and lang_code.startswith("de") else "en"
        lang_path = resource_path(f"locales/{lang_to_load}.json")
        
        try:
            if lang_path.exists():
                with open(lang_path, "r", encoding="utf-8") as f:
                    self.lang = json.load(f)
            else:
                raise FileNotFoundError(f"{lang_path} not found")
        except Exception as e:
            print(f"Error loading language file: {e}")
            # Fallback to English hardcoded if JSON missing
            self.lang = {
                "select_all": "Select/Deselect All Programs",
                "vc_select_all": "Select All VC Redists",
                "vc_redist_installs": "VC Redist Installs",
                "other_installs": "Other Installs",
                "ready": "Ready",
                "install": "Install Selected Programs",
                "cancel_install": "Cancel Installation",
                "close": "Close",
                "error": "Error",
                "winget_not_installed": "winget is not installed on this system.",
                "winget_not_ready": "winget could not be initialized. Check your internet connection and source agreements.",
                "winget_not_installed_log": "winget not installed.",
                "pre_install_cmd_fail": "Pre-installation command failed:\n{errmsg}",
                "pre_install_check_succeeded": "Pre-installation check succeeded: {msg}",
                "winget_version": "winget version: {version}",
                "checking_winget_updates": "Checking for winget updates...",
                "winget_up_to_date": "winget is up to date.",
                "no_selection": "No Selection",
                "select_one": "Please select at least one program.",
                "installation_cancelled": "Installation cancelled.",
                "installation_completed_with_errors": "Installation completed with errors.",
                "installation_completed": "Installation completed!",
                "installing": "Installing: {prog}",
                "already_installed": "{prog} is already installed.",
                "error_installing": "Error installing {prog}: {error}",
                "error_upgrading": "Error upgrading {prog}: {error}",
                "upgraded_successfully": "{prog} upgraded successfully!",
                "installation_errors": "Installation Errors",
                "installation_errors_detail": "Failed: {failed}",
                "success": "Success",
                "installation_success": "Installation successful!"
            }

    def load_programs(self):
        """Load program list from JSON."""
        prog_path = resource_path("programs.json")
        try:
            if prog_path.exists():
                with open(prog_path, "r", encoding="utf-8") as f:
                    self.programs = json.load(f)
            else:
                self.programs = []
        except Exception as e:
            messagebox.showerror("Error", f"Failed to load programs.json: {e}")
            self.programs = []

    def setup_logging(self):
        self.logger = logging.getLogger("Installer")
        self.logger.setLevel(logging.DEBUG)

        for handler in list(self.logger.handlers):
            self.logger.removeHandler(handler)
            try:
                handler.close()
            except Exception:
                pass

        gui_handler = GuiLogHandler(self.queue)
        gui_handler.setFormatter(logging.Formatter('%(asctime)s - %(levelname)s - %(message)s', '%H:%M:%S'))
        self.logger.addHandler(gui_handler)

    def setup_ui(self):
        top_frame = ttk.Frame(self.root, padding=10)
        top_frame.pack(fill='x')
        master_chk = ttk.Checkbutton(
            top_frame,
            text=self.lang.get("select_all", "Select All"),
            variable=self.select_all_var,
            command=self.toggle_all
        )
        master_chk.pack(anchor='w')

        group_frame = ttk.Frame(self.root, padding=10)
        group_frame.pack(fill='both', expand=True)

        # VC Redists column
        vc_frame = ttk.Frame(group_frame)
        vc_frame.grid(row=0, column=0, sticky="nw")
        ttk.Label(vc_frame, text=self.lang.get("vc_redist_installs", "VC Redists"), font=("Arial", 10, "bold")).pack(anchor="w")
        ttk.Checkbutton(
            vc_frame,
            text=self.lang.get("vc_select_all", "Select All VC"),
            variable=self.vc_select_all_var,
            command=self.toggle_vc_all
        ).pack(anchor="w", padx=(10, 0))
        
        for i, program in enumerate(self.programs):
            if program.get("group") == "vc":
                ttk.Checkbutton(vc_frame, text=program['name'], variable=self.vars[i]).pack(anchor="w", padx=(20, 0))

        # Other column
        other_frame = ttk.Frame(group_frame)
        other_frame.grid(row=0, column=1, sticky="ne", padx=(20, 0))
        ttk.Label(other_frame, text=self.lang.get("other_installs", "Other"), font=("Arial", 10, "bold")).pack(anchor="w")
        
        for i, program in enumerate(self.programs):
            if program.get("group") != "vc":
                ttk.Checkbutton(other_frame, text=program['name'], variable=self.vars[i]).pack(anchor="w", padx=(10, 0))

        # Progress
        progress_frame = ttk.Frame(self.root, padding=10)
        progress_frame.pack(fill='both', expand=True)
        self.progress_total = ttk.Progressbar(progress_frame, orient='horizontal', length=400, mode='determinate')
        self.progress_total.pack(pady=5)
        self.status_label = ttk.Label(progress_frame, text=self.lang.get("ready", "Ready"))
        self.status_label.pack(pady=5)

        # Log
        log_frame = ttk.Frame(self.root, padding=10)
        log_frame.pack(fill='both', expand=True)
        self.logger_text = ScrolledText(log_frame, height=12, wrap='word')
        self.logger_text.pack(fill='both', expand=True)
        
        # Tags for colored logging
        self.logger_text.tag_config("INFO", foreground="black")
        self.logger_text.tag_config("WARNING", foreground="orange")
        self.logger_text.tag_config("ERROR", foreground="red")
        self.logger_text.tag_config("DEBUG", foreground="gray")
        self.logger_text.tag_config("SUCCESS", foreground="green")

        # Buttons
        btn_frame = ttk.Frame(self.root, padding=10)
        btn_frame.pack(fill='x')
        self.install_button = ttk.Button(btn_frame, text=self.lang.get("install", "Install"), command=self.start_installation, bootstyle="success")
        self.install_button.pack(side='left', padx=5)
        self.cancel_button = ttk.Button(btn_frame, text=self.lang.get("cancel_install", "Cancel"), command=self.cancel_installation, state='disabled', bootstyle="danger")
        self.cancel_button.pack(side='left', padx=5)
        
        self.export_button = ttk.Button(btn_frame, text=self.lang.get("export_log", "Export Log"), command=self.export_log, bootstyle="info-outline")
        self.export_button.pack(side='left', padx=5)

        ttk.Button(btn_frame, text=self.lang.get("close", "Close"), command=self.on_close, bootstyle="secondary").pack(side='right', padx=5)

    def toggle_all(self):
        state = self.select_all_var.get()
        for var in self.vars:
            var.set(state)
        self.vc_select_all_var.set(state)

    def toggle_vc_all(self):
        state = self.vc_select_all_var.get()
        for i, program in enumerate(self.programs):
            if program.get("group") == "vc":
                self.vars[i].set(state)

    def cancel_installation(self):
        self.manager.cancelled = True
        # Note: InstallationManager.run_command now handles process termination
        # through its polling loop to ensure thread safety.
        self.cancel_button.config(state='disabled')

    def export_log(self):
        """Export the logger content to a .log file."""
        log_content = self.logger_text.get("1.0", "end-1c")

        file_path = filedialog.asksaveasfilename(
            defaultextension=".log",
            filetypes=[("Log files", "*.log"), ("Text files", "*.txt"), ("All files", "*.*")],
            title=self.lang.get("save_log_as", "Save Log As"),
            initialfile="installer_export.log"
        )
        
        if file_path:
            try:
                with open(file_path, "w", encoding="utf-8") as f:
                    f.write(log_content)
                messagebox.showinfo(self.lang.get("success", "Success"), self.lang.get("log_exported", "Log exported to: {path}").format(path=file_path))
            except Exception as e:
                messagebox.showerror(self.lang.get("error", "Error"), self.lang.get("export_error", "Error exporting log: {error}").format(error=str(e)))

    def process_queue(self):
        try:
            while True:
                msg = self.queue.get_nowait()
                if msg["type"] == "status":
                    self.status_label.config(text=msg["message"])
                elif msg["type"] == "log":
                    level = msg.get("level", "INFO")
                    text = msg["message"] + "\n"
                    self.logger_text.insert('end', text, level)
                    self.logger_text.see('end')
                elif msg["type"] == "progress_total":
                    self.progress_total['value'] = msg["value"]
                elif msg["type"] == "finish":
                    self.handle_finish(msg)
        except queue.Empty:
            pass
        if not self.is_closing and self.root.winfo_exists():
            self.queue_after_id = self.root.after(100, self.process_queue)

    def on_close(self):
        self.is_closing = True
        self.cancel_installation()

        if self.queue_after_id is not None:
            try:
                self.root.after_cancel(self.queue_after_id)
            except Exception:
                pass
            self.queue_after_id = None

        self.root.destroy()

    def handle_finish(self, msg):
        self.install_button.config(state='normal')
        self.cancel_button.config(state='disabled')
        if msg["success"]:
            self.status_label.config(text=self.lang["installation_completed"])
            messagebox.showinfo(self.lang["success"], self.lang["installation_success"])
        else:
            self.status_label.config(text=self.lang["installation_completed_with_errors"])
            messagebox.showwarning(
                self.lang["installation_errors"],
                self.lang["installation_errors_detail"].format(failed=msg["failed"])
            )

    def start_installation(self):
        if not self.manager.check_winget():
            messagebox.showerror(self.lang["error"], self.lang.get("winget_not_ready", self.lang["winget_not_installed"]))
            return

        selected_programs = [p for p, v in zip(self.programs, self.vars) if v.get()]
        if not selected_programs:
            messagebox.showwarning(self.lang["no_selection"], self.lang["select_one"])
            return

        self.install_button.config(state='disabled')
        self.cancel_button.config(state='normal')
        self.manager.cancelled = False
        self.progress_total['value'] = 0
        
        threading.Thread(target=self.manager.install_programs, args=(selected_programs,), daemon=True).start()

def is_admin():
    try:
        return ctypes.windll.shell32.IsUserAnAdmin()
    except Exception:
        return False

def run_as_admin():
    script = Path(sys.argv[0]).resolve()
    params = subprocess.list2cmdline([str(script), *sys.argv[1:]])
    try:
        ctypes.windll.shell32.ShellExecuteW(None, "runas", sys.executable, params, None, 1)
    except Exception as e:
        messagebox.showerror("Error", f"Failed to elevate privileges: {e}")
    sys.exit()

def main():
    if not is_admin():
        run_as_admin()

    try:
        root = ttk.Window(themename="flatly")
        root.title("Universal Runtime Installer - Improved")
        
        icon_file = resource_path("logo.ico")
        if icon_file.exists():
            try:
                root.iconbitmap(str(icon_file))
            except Exception:
                pass
        
        app = InstallerGUI(root)
        root.after(100, app.process_queue)
        root.mainloop()
    except Exception:
        error_details = traceback.format_exc()
        messagebox.showerror("Fatal Error", f"An unhandled exception occurred:\n{error_details}")
        sys.exit(1)

if __name__ == "__main__":
    main()
