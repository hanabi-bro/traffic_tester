"""
Rich output module for traffic test tool.
Provides color-coded console output with threshold-based warnings.
"""

from __future__ import annotations

from rich.console import Console
from rich.text import Text
from rich.style import Style


class TableTrafficOutput:
    """
    Table format console output handler.
    Outputs data in a formatted table: TimeStamp, SrcAddr, DstAddr, Action, Proto, Port, BPS, Total, Message
    """
    
    def __init__(self, threshold: int = 1000):
        """
        Initialize table output handler.
        
        Args:
            threshold: Data transfer threshold in bytes per second for color coding
        """
        self.console = Console()
        self.threshold = threshold
        self.header_printed = False
        
        # Define styles
        self.style_normal = Style(color="white")
        self.style_error = Style(color="red", bold=True)
        self.style_timeout = Style(color="red", bold=True)
        self.style_warning = Style(color="orange1", bold=True)
        self.style_connect = Style(color="green")
        self.style_disconnect = Style(color="yellow")
        self.style_header = Style(color="cyan", bold=True)
        
        # Print table header once
        self._print_header()
    
    def _print_header(self):
        """Print table header."""
        if not self.header_printed:
            header = f"{'TimeStamp':<13} {'SrcAddr':<15} {'DstAddr':<15} {'Action':<9} {'Proto':<7} {'Port':<6} {'BPS':<8} {'Total':<8} {'Message'}"
            self.console.print(header, style=self.style_header)
            self.header_printed = True
    
    def _format_bytes(self, bytes_val: int) -> str:
        """Format bytes with appropriate unit."""
        if bytes_val >= 1_000_000_000:
            return f"{bytes_val/1_000_000_000:.1f}G"
        elif bytes_val >= 1_000_000:
            return f"{bytes_val/1_000_000:.1f}M"
        elif bytes_val >= 1_000:
            return f"{bytes_val/1_000:.1f}K"
        else:
            return str(bytes_val)
    
    def _format_bps(self, bps_val: float) -> str:
        """Format bits per second with appropriate unit."""
        if bps_val >= 1_000_000_000:
            return f"{bps_val/1_000_000_000:.1f}G"
        elif bps_val >= 1_000_000:
            return f"{bps_val/1_000_000:.1f}M"
        elif bps_val >= 1_000:
            return f"{bps_val/1_000:.1f}K"
        else:
            return f"{bps_val:.0f}"
    
    def _get_style_for_event(self, event_type: str, bps_sent: float, bps_recv: float, role: str, mode: str) -> Style:
        """Get appropriate style based on event type and data rates."""
        if event_type == "ERROR":
            return self.style_error
        elif event_type == "TIMEOUT":
            return self.style_timeout
        elif event_type == "CONNECT":
            return self.style_connect
        elif event_type == "DISCONNECT":
            return self.style_disconnect
        elif event_type == "DATA":
            # Check transfer rates based on mode and role
            mode = mode.lower()
            role = role.lower()
            
            if mode == "download":
                # Download mode: server sends, client receives
                if role == "client":
                    # Client: check receive rate
                    if bps_recv < self.threshold:
                        return self.style_warning
                else:
                    # Server: check send rate
                    if bps_sent < self.threshold:
                        return self.style_warning
            elif mode == "upload":
                # Upload mode: client sends, server receives
                if role == "client":
                    # Client: check send rate
                    if bps_sent < self.threshold:
                        return self.style_warning
                else:
                    # Server: check receive rate
                    if bps_recv < self.threshold:
                        return self.style_warning
            else:
                # Both mode or unspecified: check both rates
                if bps_sent < self.threshold or bps_recv < self.threshold:
                    return self.style_warning
        
        return self.style_normal
    
    def format_table_row(self, row: dict) -> tuple[str, Style]:
        """
        Format a CSV row as table row with appropriate styling.
        
        Args:
            row: Dictionary containing CSV field values
            
        Returns:
            Tuple of (formatted_row_string, style)
        """
        # Extract fields
        datetime_str = row.get("datetime", "")
        event_type = row.get("event_type", "")
        proto = row.get("proto", "")
        server_ip = row.get("server_ip", "")
        server_port = str(row.get("server_port", ""))
        client_ip = row.get("client_ip", "")
        client_port = str(row.get("client_port", ""))
        bps_sent = float(row.get("bps_sent", 0))
        bps_recv = float(row.get("bps_recv", 0))
        bytes_sent = int(row.get("bytes_sent", 0))
        bytes_recv = int(row.get("bytes_recv", 0))
        message = row.get("message", "")
        role = row.get("role", "")
        mode = row.get("mode", "")
        
        # Extract timestamp (HH:MM:SS.mmm format)
        try:
            from datetime import datetime
            dt = datetime.strptime(datetime_str, "%Y-%m-%d %H:%M:%S.%f")
            timestamp = dt.strftime("%H:%M:%S.%f")[:-3]  # Keep milliseconds
        except (ValueError, IndexError):
            timestamp = datetime_str.split(" ")[1] if " " in datetime_str else datetime_str
        
        # Determine source and destination based on role
        if role == "client":
            src_addr = f"{client_ip}:{client_port}"
            dst_addr = f"{server_ip}:{server_port}"
        else:
            src_addr = f"{server_ip}:{server_port}"
            dst_addr = f"{client_ip}:{client_port}"
        
        # Truncate addresses if too long
        src_addr = src_addr[:15]
        dst_addr = dst_addr[:15]
        
        # Determine BPS and Total based on role and mode
        if role == "client":
            if mode == "download":
                bps = bps_recv
                total = bytes_recv
            elif mode == "upload":
                bps = bps_sent
                total = bytes_sent
            else:  # both
                bps = max(bps_sent, bps_recv)
                total = max(bytes_sent, bytes_recv)
        else:  # server
            if mode == "download":
                bps = bps_sent
                total = bytes_sent
            elif mode == "upload":
                bps = bps_recv
                total = bytes_recv
            else:  # both
                bps = max(bps_sent, bps_recv)
                total = max(bytes_sent, bytes_recv)
        
        # Format values
        bps_str = self._format_bps(bps) if bps > 0 else ""
        total_str = self._format_bytes(total) if total > 0 else ""
        
        # Shorten protocol name if needed
        proto_display = proto
        if proto == "HTTPS":
            proto_display = "HTTPS"
        elif proto == "HTTP":
            proto_display = "HTTP"
        
        # Format message (truncate if too long)
        message_display = message[:30] if len(message) > 30 else message
        
        # Build table row
        table_row = f"{timestamp:<13} {src_addr:<15} {dst_addr:<15} {event_type:<9} {proto_display:<7} {server_port:<6} {bps_str:<8} {total_str:<8} {message_display}"
        
        # Get style
        style = self._get_style_for_event(event_type, bps_sent, bps_recv, role, mode)
        
        return table_row, style
    
    def print_row(self, row: dict) -> None:
        """
        Print a CSV row in table format with appropriate coloring.
        
        Args:
            row: Dictionary containing CSV field values
        """
        formatted_row, style = self.format_table_row(row)
        # Debug: print the formatted row to see what's happening
        # self.console.print(f"DEBUG: {formatted_row}", style="yellow")
        self.console.print(formatted_row, style=style)
    
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
    
    def __init__(self, threshold: int = 1000, use_table_format: bool = False):
        """
        Initialize rich output handler.
        
        Args:
            threshold: Data transfer threshold in bytes per second for orange warning
            use_table_format: If True, use table format instead of CSV format
        """
        self.threshold = threshold
        self.use_table_format = use_table_format
        
        if use_table_format:
            self.table_output = TableTrafficOutput(threshold)
        else:
            self.console = Console()
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
        if self.use_table_format:
            self.table_output.print_row(row)
        else:
            formatted_text = self.format_csv_row(row)
            self.console.print(formatted_text)
    
    def print_message(self, message: str, event_type: str = "INFO") -> None:
        """
        Print a simple message with appropriate coloring.
        
        Args:
            message: Message to print
            event_type: Event type for color determination
        """
        if self.use_table_format:
            self.table_output.print_message(message, event_type)
        else:
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
