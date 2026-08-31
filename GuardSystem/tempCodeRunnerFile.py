import customtkinter as ctk
from ui.main_window import MainWindow

def main():
    # Set the global appearance
    ctk.set_appearance_mode("Dark")
    ctk.set_default_color_theme("blue")

    # Initialize the main UI window
    app = MainWindow()
    
    # Graceful exit handling can be attached here
    try:
        app.mainloop()
    except KeyboardInterrupt:
        print("Shutting down GUARD system...")

if __name__ == "__main__":
    main()