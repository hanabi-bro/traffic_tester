"""
Rich output module for traffic test tool.
Provides color-coded console output with threshold-based warnings.
"""

from __future__ import annotations

from rich.console import Console
from rich.text import Text
from rich.style import Style


class RichTrafficOutput:
    """
    Rich console output handler with color coding based on event types and thresholds.
    
    Color scheme:
    - Normal output: White (default)
    - ERROR events: Red
    - TIMEOUT events: Red  
    - DATA events below threshold: Orange
    - Other events: White
    """
    
    def __init__(self, threshold: int = 1000):
        """
        Initialize rich output handler.
        
        Args:
            threshold: Data transfer threshold in bytes per second for orange warning
        """
        self.console = Console()
        self.threshold = threshold
        
        # Define styles
        self.style_normal = Style(color="white")
        self.style_error = Style(color="red", bold=True)
        self.style_timeout = Style(color="red", bold=True)
        self.style_warning = Style(color="orange1", bold=True)
        self.style_connect = Style(color="green")
        self.style_disconnect = Style(color="yellow")
    
    def format_csv_row(self, row: dict) -> Text:
        """
        Format a CSV row with appropriate coloring based on event type and data rates.
        
        Args:
            row: Dictionary containing CSV field values
            
        Returns:
            Rich Text object with appropriate styling
        """
        event_type = row.get("event_type", "")
        bps_sent = float(row.get("bps_sent", 0))
        bps_recv = float(row.get("bps_recv", 0))
        
        # Determine base style based on event type
        if event_type == "ERROR":
            base_style = self.style_error
        elif event_type == "TIMEOUT":
            base_style = self.style_timeout
        elif event_type == "CONNECT":
            base_style = self.style_connect
        elif event_type == "DISCONNECT":
            base_style = self.style_disconnect
        elif event_type == "DATA":
            # Check transfer rates based on mode and role
            mode = row.get("mode", "").lower()
            role = row.get("role", "").lower()
            
            if mode == "download":
                # Download mode: server sends, client receives
                if role == "client":
                    # Client: check receive rate
                    if bps_recv < self.threshold:
                        base_style = self.style_warning
                    else:
                        base_style = self.style_normal
                else:
                    # Server: check send rate
                    if bps_sent < self.threshold:
                        base_style = self.style_warning
                    else:
                        base_style = self.style_normal
            elif mode == "upload":
                # Upload mode: client sends, server receives
                if role == "client":
                    # Client: check send rate
                    if bps_sent < self.threshold:
                        base_style = self.style_warning
                    else:
                        base_style = self.style_normal
                else:
                    # Server: check receive rate
                    if bps_recv < self.threshold:
                        base_style = self.style_warning
                    else:
                        base_style = self.style_normal
            else:
                # Both mode or unspecified: check both rates
                if bps_sent < self.threshold or bps_recv < self.threshold:
                    base_style = self.style_warning
                else:
                    base_style = self.style_normal
        else:
            base_style = self.style_normal
        
        # Create formatted text
        # CSV field order from logger.py
        fields = [
            "datetime", "event_type", "proto", "server_ip", "server_port",
            "client_ip", "client_port", "elapsed_sec", "bytes_sent", "bytes_recv",
            "bps_sent", "bps_recv", "message", "pkt_seq", "pkt_loss", "pkt_ooo", "mode", "role"
        ]
        
        values = []
        for field in fields:
            value = str(row.get(field, ""))
            
            # Special formatting for numeric fields
            if field in ["bps_sent", "bps_recv"]:
                try:
                    bps = float(value)
                    if bps > 0:
                        # Format with appropriate unit
                        if bps >= 1_000_000:
                            value = f"{bps/1_000_000:.1f}M"
                        elif bps >= 1_000:
                            value = f"{bps/1_000:.1f}K"
                        else:
                            value = f"{bps:.0f}"
                except ValueError:
                    pass
            elif field in ["bytes_sent", "bytes_recv"]:
                try:
                    bytes_val = int(value)
                    if bytes_val > 0:
                        # Format with appropriate unit
                        if bytes_val >= 1_000_000_000:
                            value = f"{bytes_val/1_000_000_000:.1f}G"
                        elif bytes_val >= 1_000_000:
                            value = f"{bytes_val/1_000_000:.1f}M"
                        elif bytes_val >= 1_000:
                            value = f"{bytes_val/1_000:.1f}K"
                        else:
                            value = str(bytes_val)
                except ValueError:
                    pass
            
            values.append(value)
        
        # Create text with styling
        text = Text(",".join(values), style=base_style)
        
        # Add special highlighting for warning conditions
        if event_type == "DATA" and base_style == self.style_warning:
            # Highlight the low transfer rates
            text = Text()
            for i, (field, value) in enumerate(zip(fields, values)):
                if i > 0:
                    text.append(",")
                
                if field in ["bps_sent", "bps_recv"]:
                    try:
                        bps = float(row.get(field, 0))
                        if bps > 0 and bps < self.threshold:
                            text.append(value, style=self.style_warning)
                        else:
                            text.append(value, style=self.style_normal)
                    except ValueError:
                        text.append(value, style=self.style_normal)
                else:
                    text.append(value, style=base_style)
        
        return text
    
    def print_row(self, row: dict) -> None:
        """
        Print a CSV row with appropriate coloring.
        
        Args:
            row: Dictionary containing CSV field values
        """
        formatted_text = self.format_csv_row(row)
        self.console.print(formatted_text)
    
    def print_message(self, message: str, event_type: str = "INFO") -> None:
        """
        Print a simple message with appropriate coloring.
        
        Args:
            message: Message to print
            event_type: Event type for color determination
        """
        if event_type == "ERROR":
            style = self.style_error
        elif event_type == "TIMEOUT":
            style = self.style_timeout
        elif event_type == "CONNECT":
            style = self.style_connect
        elif event_type == "DISCONNECT":
            style = self.style_disconnect
        else:
            style = self.style_normal
        
        self.console.print(message, style=style)
