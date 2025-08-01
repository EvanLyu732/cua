import pyautogui
from pynput.mouse import Button, Controller as MouseController
from pynput.keyboard import Key, Controller as KeyboardController
import time
import base64
from io import BytesIO
from typing import Optional, Dict, Any, List, Tuple
from ctypes import byref, c_void_p, POINTER
from AppKit import NSWorkspace  # type: ignore
import AppKit
from Quartz.CoreGraphics import *  # type: ignore
from Quartz.CoreGraphics import CGPoint, CGSize  # type: ignore
import Foundation
from ApplicationServices import (
    AXUIElementCreateSystemWide,  # type: ignore
    AXUIElementCreateApplication,  # type: ignore
    AXUIElementCopyAttributeValue,  # type: ignore
    AXUIElementCopyAttributeValues,  # type: ignore
    kAXFocusedWindowAttribute,  # type: ignore
    kAXWindowsAttribute,  # type: ignore
    kAXMainWindowAttribute,  # type: ignore
    kAXChildrenAttribute,  # type: ignore
    kAXRoleAttribute,  # type: ignore
    kAXTitleAttribute,  # type: ignore
    kAXValueAttribute,  # type: ignore
    kAXDescriptionAttribute,  # type: ignore
    kAXEnabledAttribute,  # type: ignore
    kAXPositionAttribute,  # type: ignore
    kAXSizeAttribute,  # type: ignore
    kAXErrorSuccess,  # type: ignore
    AXValueGetType,  # type: ignore
    kAXValueCGSizeType,  # type: ignore
    kAXValueCGPointType,  # type: ignore
    kAXValueCFRangeType,  # type: ignore
    AXUIElementGetTypeID,  # type: ignore
    AXValueGetValue,  # type: ignore
    kAXVisibleChildrenAttribute,  # type: ignore
    kAXRoleDescriptionAttribute,  # type: ignore
    kAXFocusedApplicationAttribute,  # type: ignore
    kAXFocusedUIElementAttribute,  # type: ignore
    kAXSelectedTextAttribute,  # type: ignore
    kAXSelectedTextRangeAttribute,  # type: ignore
)
import objc
import re
import json
import copy
import asyncio
from .base import BaseAccessibilityHandler, BaseAutomationHandler
import logging

logger = logging.getLogger(__name__)

# Constants for accessibility API
kAXErrorSuccess = 0
kAXRoleAttribute = "AXRole"
kAXTitleAttribute = "AXTitle"
kAXValueAttribute = "AXValue"
kAXWindowsAttribute = "AXWindows"
kAXFocusedAttribute = "AXFocused"
kAXPositionAttribute = "AXPosition"
kAXSizeAttribute = "AXSize"
kAXChildrenAttribute = "AXChildren"
kAXMenuBarAttribute = "AXMenuBar"
kAXMenuBarItemAttribute = "AXMenuBarItem"

# Constants for window properties
kCGWindowLayer = "kCGWindowLayer"  # Z-order information (lower values are higher in the stack)
kCGWindowAlpha = "kCGWindowAlpha"  # Window opacity

# Constants for application activation options
NSApplicationActivationOptions = {
    "regular": 0,  # Default activation
    "bringing_all_windows_forward": 1 << 0,  # NSApplicationActivateAllWindows
    "ignoring_other_apps": 1 << 1  # NSApplicationActivateIgnoringOtherApps
}

def CFAttributeToPyObject(attrValue):
    def list_helper(list_value):
        list_builder = []
        for item in list_value:
            list_builder.append(CFAttributeToPyObject(item))
        return list_builder

    def number_helper(number_value):
        success, int_value = Foundation.CFNumberGetValue(  # type: ignore
            number_value, Foundation.kCFNumberIntType, None  # type: ignore
        )
        if success:
            return int(int_value)

        success, float_value = Foundation.CFNumberGetValue(  # type: ignore
            number_value, Foundation.kCFNumberDoubleType, None  # type: ignore
        )
        if success:
            return float(float_value)
        return None

    def axuielement_helper(element_value):
        return element_value

    cf_attr_type = Foundation.CFGetTypeID(attrValue)  # type: ignore
    cf_type_mapping = {
        Foundation.CFStringGetTypeID(): str,  # type: ignore
        Foundation.CFBooleanGetTypeID(): bool,  # type: ignore
        Foundation.CFArrayGetTypeID(): list_helper,  # type: ignore
        Foundation.CFNumberGetTypeID(): number_helper,  # type: ignore
        AXUIElementGetTypeID(): axuielement_helper,  # type: ignore
    }
    try:
        return cf_type_mapping[cf_attr_type](attrValue)
    except KeyError:
        # did not get a supported CF type. Move on to AX type
        pass

    ax_attr_type = AXValueGetType(attrValue)
    ax_type_map = {
        kAXValueCGSizeType: Foundation.NSSizeFromString,  # type: ignore
        kAXValueCGPointType: Foundation.NSPointFromString,  # type: ignore
        kAXValueCFRangeType: Foundation.NSRangeFromString,  # type: ignore
    }
    try:
        search_result = re.search("{.*}", attrValue.description())
        if search_result:
            extracted_str = search_result.group()
            return tuple(ax_type_map[ax_attr_type](extracted_str))
        return None
    except KeyError:
        return None


def element_attribute(element, attribute):
    if attribute == kAXChildrenAttribute:
        err, value = AXUIElementCopyAttributeValues(element, attribute, 0, 999, None)
        if err == kAXErrorSuccess:
            if isinstance(value, Foundation.NSArray):  # type: ignore
                return CFAttributeToPyObject(value)
            else:
                return value
    err, value = AXUIElementCopyAttributeValue(element, attribute, None)
    if err == kAXErrorSuccess:
        if isinstance(value, Foundation.NSArray):  # type: ignore
            return CFAttributeToPyObject(value)
        else:
            return value
    return None


def element_value(element, type):
    err, value = AXValueGetValue(element, type, None)
    if err == True:
        return value
    return None


class UIElement:
    def __init__(self, element, offset_x=0, offset_y=0, max_depth=None, parents_visible_bbox=None):
        self.ax_element = element
        self.content_identifier = ""
        self.identifier = ""
        self.name = ""
        self.children = []
        self.description = ""
        self.role_description = ""
        self.value = None
        self.max_depth = max_depth

        # Set role
        self.role = element_attribute(element, kAXRoleAttribute)
        if self.role is None:
            self.role = "No role"

        # Set name
        self.name = element_attribute(element, kAXTitleAttribute)
        if self.name is not None:
            # Convert tuple to string if needed
            if isinstance(self.name, tuple):
                self.name = str(self.name[0]) if self.name else ""
            self.name = self.name.replace(" ", "_")

        # Set enabled
        self.enabled = element_attribute(element, kAXEnabledAttribute)
        if self.enabled is None:
            self.enabled = False

        # Set position and size
        position = element_attribute(element, kAXPositionAttribute)
        size = element_attribute(element, kAXSizeAttribute)
        start_position = element_value(position, kAXValueCGPointType)

        if self.role == "AXWindow" and start_position is not None:
            offset_x = start_position.x
            offset_y = start_position.y

        self.absolute_position = copy.copy(start_position)
        self.position = start_position
        if self.position is not None:
            self.position.x -= max(0, offset_x)
            self.position.y -= max(0, offset_y)
        self.size = element_value(size, kAXValueCGSizeType)

        self._set_bboxes(parents_visible_bbox)

        # Set component center
        if start_position is None or self.size is None:
            print("Position is None")
            return
        self.center = (
            start_position.x + offset_x + self.size.width / 2,
            start_position.y + offset_y + self.size.height / 2,
        )

        self.description = element_attribute(element, kAXDescriptionAttribute)
        self.role_description = element_attribute(element, kAXRoleDescriptionAttribute)
        attribute_value = element_attribute(element, kAXValueAttribute)

        # Set value
        self.value = attribute_value
        if attribute_value is not None:
            if isinstance(attribute_value, Foundation.NSArray):  # type: ignore
                self.value = []
                for value in attribute_value:
                    self.value.append(value)
            # Check if it's an accessibility element by checking its type ID
            elif Foundation.CFGetTypeID(attribute_value) == AXUIElementGetTypeID():  # type: ignore
                self.value = UIElement(attribute_value, offset_x, offset_y)

        # Set children
        if self.max_depth is None or self.max_depth > 0:
            self.children = self._get_children(element, start_position, offset_x, offset_y)
        else:
            self.children = []

        self.calculate_hashes()

    def _set_bboxes(self, parents_visible_bbox):
        if not self.absolute_position or not self.size:
            self.bbox = None
            self.visible_bbox = None
            return
        self.bbox = [
            int(self.absolute_position.x),
            int(self.absolute_position.y),
            int(self.absolute_position.x + self.size.width),
            int(self.absolute_position.y + self.size.height),
        ]
        if parents_visible_bbox:
            # check if not intersected
            if (
                self.bbox[0] > parents_visible_bbox[2]
                or self.bbox[1] > parents_visible_bbox[3]
                or self.bbox[2] < parents_visible_bbox[0]
                or self.bbox[3] < parents_visible_bbox[1]
            ):
                self.visible_bbox = None
            else:
                self.visible_bbox = [
                    int(max(self.bbox[0], parents_visible_bbox[0])),
                    int(max(self.bbox[1], parents_visible_bbox[1])),
                    int(min(self.bbox[2], parents_visible_bbox[2])),
                    int(min(self.bbox[3], parents_visible_bbox[3])),
                ]
        else:
            self.visible_bbox = self.bbox

    def _get_children(self, element, start_position, offset_x, offset_y):
        children = element_attribute(element, kAXChildrenAttribute)
        visible_children = element_attribute(element, kAXVisibleChildrenAttribute)
        found_children = []
        if children is not None:
            found_children.extend(children)
        else:
            if visible_children is not None:
                found_children.extend(visible_children)

        result = []
        if self.max_depth is None or self.max_depth > 0:
            for child in found_children:
                child = UIElement(
                    child,
                    offset_x,
                    offset_y,
                    self.max_depth - 1 if self.max_depth is not None else None,
                    self.visible_bbox,
                )
                result.append(child)
        return result

    def calculate_hashes(self):
        self.identifier = self.component_hash()
        self.content_identifier = self.children_content_hash(self.children)

    def component_hash(self):
        if self.position is None or self.size is None:
            return ""
        position_string = f"{self.position.x:.0f};{self.position.y:.0f}"
        size_string = f"{self.size.width:.0f};{self.size.height:.0f}"
        enabled_string = str(self.enabled)
        # Ensure role is a string
        role_string = ""
        if self.role is not None:
            role_string = str(self.role[0]) if isinstance(self.role, tuple) else str(self.role)
        return self.hash_from_string(position_string + size_string + enabled_string + role_string)

    def hash_from_string(self, string):
        if string is None or string == "":
            return ""
        from hashlib import md5

        return md5(string.encode()).hexdigest()

    def children_content_hash(self, children):
        if len(children) == 0:
            return ""
        all_content_hashes = []
        all_hashes = []
        for child in children:
            all_content_hashes.append(child.content_identifier)
            all_hashes.append(child.identifier)
        all_content_hashes.sort()
        if len(all_content_hashes) == 0:
            return ""
        content_hash = self.hash_from_string("".join(all_content_hashes))
        content_structure_hash = self.hash_from_string("".join(all_hashes))
        return self.hash_from_string(content_hash.join(content_structure_hash))

    def to_dict(self):
        def children_to_dict(children):
            result = []
            for child in children:
                result.append(child.to_dict())
            return result

        value = self.value
        if isinstance(value, UIElement):
            value = json.dumps(value.to_dict(), indent=4)
        elif isinstance(value, AppKit.NSDate):  # type: ignore
            value = str(value)

        if self.absolute_position is not None:
            absolute_position = f"{self.absolute_position.x:.2f};{self.absolute_position.y:.2f}"
        else:
            absolute_position = ""

        if self.position is not None:
            position = f"{self.position.x:.2f};{self.position.y:.2f}"
        else:
            position = ""

        if self.size is not None:
            size = f"{self.size.width:.0f};{self.size.height:.0f}"
        else:
            size = ""
            
        return {
            "id": self.identifier,
            "name": self.name,
            "role": self.role,
            "description": self.description,
            "role_description": self.role_description,
            "value": value,
            "absolute_position": absolute_position,
            "position": position,
            "size": size,
            "enabled": self.enabled,
            "bbox": self.bbox,
            "visible_bbox": self.visible_bbox,
            "children": children_to_dict(self.children),
        }


import Quartz
from AppKit import NSWorkspace, NSRunningApplication
from pathlib import Path

def get_all_windows_zorder():
    window_list = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionOnScreenOnly,
        Quartz.kCGNullWindowID
    )
    z_order = {window['kCGWindowNumber']: z_index for z_index, window in enumerate(window_list[::-1])}
    window_list_all = Quartz.CGWindowListCopyWindowInfo(
        Quartz.kCGWindowListOptionAll,
        Quartz.kCGNullWindowID
    )
    windows = []
    for window in window_list_all:
        window_id = window.get('kCGWindowNumber', 0)
        window_name = window.get('kCGWindowName', '')
        window_pid = window.get('kCGWindowOwnerPID', 0)
        window_bounds = window.get('kCGWindowBounds', {})
        window_owner = window.get('kCGWindowOwnerName', '')
        window_is_on_screen = window.get('kCGWindowIsOnscreen', False)
        layer = window.get('kCGWindowLayer', 0)
        opacity = window.get('kCGWindowAlpha', 1.0)
        z_index = z_order.get(window_id, -1)
        if window_name == "Dock" and window_owner == "Dock":
            role = "dock"
        elif window_name == "Menubar" and window_owner == "Window Server":
            role = "menubar"
        elif window_owner in ["Window Server", "Dock"]:
            role = "desktop"
        else:
            role = "app"
        if window_bounds:
            windows.append({
                "id": window_id,
                "name": window_name or "Unnamed Window",
                "pid": window_pid,
                "owner": window_owner,
                "role": role,
                "is_on_screen": window_is_on_screen,
                "bounds": {
                    "x": window_bounds.get('X', 0),
                    "y": window_bounds.get('Y', 0),
                    "width": window_bounds.get('Width', 0),
                    "height": window_bounds.get('Height', 0)
                },
                "layer": layer,
                "z_index": z_index,
                "opacity": opacity
            })
    windows = sorted(windows, key=lambda x: x["z_index"])
    return windows

def get_app_info(app):
    return {
        "name": app.localizedName(),
        "bundle_id": app.bundleIdentifier(),
        "pid": app.processIdentifier(),
        "active": app.isActive(),
        "hidden": app.isHidden(),
        "terminated": app.isTerminated(),
    }

def get_menubar_items(active_app_pid=None):
    menubar_items = []
    if active_app_pid is None:
        frontmost_app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if frontmost_app:
            active_app_pid = frontmost_app.processIdentifier()
        else:
            return menubar_items
    app_element = AXUIElementCreateApplication(active_app_pid)
    if app_element is None:
        return menubar_items
    menubar = element_attribute(app_element, kAXMenuBarAttribute)
    if menubar is None:
        return menubar_items
    children = element_attribute(menubar, kAXChildrenAttribute)
    if children is None:
        return menubar_items
    for i, item in enumerate(children):
        title = element_attribute(item, kAXTitleAttribute) or "Untitled"
        bounds = {"x": 0, "y": 0, "width": 0, "height": 0}
        position_value = element_attribute(item, kAXPositionAttribute)
        if position_value:
            position_value = element_value(position_value, kAXValueCGPointType)
            bounds["x"] = getattr(position_value, 'x', 0)
            bounds["y"] = getattr(position_value, 'y', 0)
        size_value = element_attribute(item, kAXSizeAttribute)
        if size_value:
            size_value = element_value(size_value, kAXValueCGSizeType)
            bounds["width"] = getattr(size_value, 'width', 0)
            bounds["height"] = getattr(size_value, 'height', 0)
        menubar_items.append({
            "title": title,
            "bounds": bounds,
            "index": i,
            "app_pid": active_app_pid
        })
    return menubar_items

def get_dock_items():
    dock_items = []
    dock_pid = None
    running_apps = NSWorkspace.sharedWorkspace().runningApplications()
    for app in running_apps:
        if app.localizedName() == "Dock" and app.bundleIdentifier() == "com.apple.dock":
            dock_pid = app.processIdentifier()
            break
    if dock_pid is None:
        return dock_items
    dock_element = AXUIElementCreateApplication(dock_pid)
    if dock_element is None:
        return dock_items
    dock_list = element_attribute(dock_element, kAXChildrenAttribute)
    if dock_list is None or len(dock_list) == 0:
        return dock_items
    dock_app_list = None
    for child in dock_list:
        role = element_attribute(child, kAXRoleAttribute)
        if role == "AXList":
            dock_app_list = child
            break
    if dock_app_list is None:
        return dock_items
    items = element_attribute(dock_app_list, kAXChildrenAttribute)
    if items is None:
        return dock_items
    for i, item in enumerate(items):
        title = element_attribute(item, kAXTitleAttribute) or "Untitled"
        description = element_attribute(item, kAXDescriptionAttribute) or ""
        role = element_attribute(item, kAXRoleAttribute) or ""
        subrole = element_attribute(item, "AXSubrole") or ""
        bounds = {"x": 0, "y": 0, "width": 0, "height": 0}
        position_value = element_attribute(item, kAXPositionAttribute)
        if position_value:
            position_value = element_value(position_value, kAXValueCGPointType)
            bounds["x"] = getattr(position_value, 'x', 0)
            bounds["y"] = getattr(position_value, 'y', 0)
        size_value = element_attribute(item, kAXSizeAttribute)
        if size_value:
            size_value = element_value(size_value, kAXValueCGSizeType)
            bounds["width"] = getattr(size_value, 'width', 0)
            bounds["height"] = getattr(size_value, 'height', 0)
        item_type = "unknown"
        if subrole == "AXApplicationDockItem":
            item_type = "application"
        elif subrole == "AXFolderDockItem":
            item_type = "folder"
        elif subrole == "AXDocumentDockItem":
            item_type = "document"
        elif subrole == "AXSeparatorDockItem" or role == "AXSeparator":
            item_type = "separator"
        elif "trash" in title.lower():
            item_type = "trash"
        dock_items.append({
            "title": title,
            "description": description,
            "bounds": bounds,
            "index": i,
            "type": item_type,
            "role": role,
            "subrole": subrole
        })
    return dock_items

class MacOSAccessibilityHandler(BaseAccessibilityHandler):
    def get_desktop_state(self):
        windows = [w for w in get_all_windows_zorder() if w.get("is_on_screen")]
        running_apps = self.get_running_apps()
        applications = []
        pid_to_window_ids = {}
        # Build a mapping: pid -> list of AX window trees
        pid_to_ax_trees = {}
        for app in running_apps:
            pid = app.processIdentifier()
            try:
                app_elem = AXUIElementCreateApplication(pid)
                err, app_windows = AXUIElementCopyAttributeValue(app_elem, kAXWindowsAttribute, None)
                trees = []
                if err == kAXErrorSuccess and app_windows:
                    for ax_win in app_windows:
                        try:
                            trees.append(UIElement(ax_win).to_dict())
                        except Exception as e:
                            trees.append({"error": str(e)})
                pid_to_ax_trees[pid] = trees
            except Exception as e:
                pid_to_ax_trees[pid] = [{"error": str(e)}]
        # Attach children by pid and index (order)
        pid_to_idx = {}
        for win in windows:
            pid = win["pid"]
            idx = pid_to_idx.get(pid, 0)
            ax_trees = pid_to_ax_trees.get(pid, [])
            win["children"] = ax_trees[idx]["children"] if idx < len(ax_trees) and "children" in ax_trees[idx] else []
            pid_to_idx[pid] = idx + 1
            pid_to_window_ids.setdefault(pid, []).append(win["id"])
        for app in running_apps:
            info = get_app_info(app)
            app_pid = info["pid"]
            applications.append({
                "info": info,
                "windows": pid_to_window_ids.get(app_pid, [])
            })
        menubar_items = get_menubar_items()
        dock_items = get_dock_items()
        return {
            "applications": applications,
            "windows": windows,
            "menubar_items": menubar_items,
            "dock_items": dock_items
        }

    def get_application_windows(self, pid: int):
        """Get all windows for a specific application."""
        try:
            app = AXUIElementCreateApplication(pid)
            err, windows = AXUIElementCopyAttributeValue(app, kAXWindowsAttribute, None)
            if err == kAXErrorSuccess and windows:
                if isinstance(windows, Foundation.NSArray):  # type: ignore
                    return windows
            return []
        except:
            return []

    def get_all_windows(self):
        """Get all visible windows in the system."""
        try:
            windows = []
            running_apps = self.get_running_apps()

            for app in running_apps:
                try:
                    app_name = app.localizedName()
                    pid = app.processIdentifier()

                    # Skip system processes and background apps
                    if not app.activationPolicy() == 0:  # NSApplicationActivationPolicyRegular
                        continue

                    # Get application windows
                    app_windows = self.get_application_windows(pid)

                    windows.append(
                        {
                            "app_name": app_name,
                            "pid": pid,
                            "frontmost": app.isActive(),
                            "has_windows": len(app_windows) > 0,
                            "windows": app_windows,
                        }
                    )
                except:
                    continue

            return windows
        except:
            return []

    def get_running_apps(self):
        # From NSWorkspace.runningApplications docs: https://developer.apple.com/documentation/appkit/nsworkspace/runningapplications
        # "Similar to the NSRunningApplication class’s properties, this property will only change when the main run loop runs in a common mode"
        # So we need to run the main run loop to get the latest running applications
        Foundation.CFRunLoopRunInMode(Foundation.kCFRunLoopDefaultMode, 0.1, False)  # type: ignore
        return NSWorkspace.sharedWorkspace().runningApplications()

    def get_ax_attribute(self, element, attribute):
        return element_attribute(element, attribute)

    def serialize_node(self, element):
        # Create a serializable dictionary representation of an accessibility element
        result = {}

        # Get basic attributes
        result["role"] = self.get_ax_attribute(element, kAXRoleAttribute)
        result["title"] = self.get_ax_attribute(element, kAXTitleAttribute)
        result["value"] = self.get_ax_attribute(element, kAXValueAttribute)

        # Get position and size if available
        position = self.get_ax_attribute(element, kAXPositionAttribute)
        if position:
            try:
                position_dict = {"x": position[0], "y": position[1]}
                result["position"] = position_dict
            except (IndexError, TypeError):
                pass

        size = self.get_ax_attribute(element, kAXSizeAttribute)
        if size:
            try:
                size_dict = {"width": size[0], "height": size[1]}
                result["size"] = size_dict
            except (IndexError, TypeError):
                pass

        return result

    async def get_accessibility_tree(self) -> Dict[str, Any]:        
        try:
            desktop_state = self.get_desktop_state()
            return {
                "success": True,
                **desktop_state
            } 

        except Exception as e:
            return {"success": False, "error": str(e)}

    async def find_element(
        self, role: Optional[str] = None, title: Optional[str] = None, value: Optional[str] = None
    ) -> Dict[str, Any]:
        try:
            system = AXUIElementCreateSystemWide()

            def match_element(element):
                if role and self.get_ax_attribute(element, kAXRoleAttribute) != role:
                    return False
                if title and self.get_ax_attribute(element, kAXTitleAttribute) != title:
                    return False
                if value and str(self.get_ax_attribute(element, kAXValueAttribute)) != value:
                    return False
                return True

            def search_tree(element):
                if match_element(element):
                    return self.serialize_node(element)

                children = self.get_ax_attribute(element, kAXChildrenAttribute)
                if children:
                    for child in children:
                        result = search_tree(child)
                        if result:
                            return result
                return None

            element = search_tree(system)
            return {"success": True, "element": element}

        except Exception as e:
            return {"success": False, "error": str(e)}

class MacOSAutomationHandler(BaseAutomationHandler):
    # Mouse Actions
    mouse = MouseController()
    keyboard = KeyboardController()
    
    async def mouse_down(self, x: Optional[int] = None, y: Optional[int] = None, button: str = "left") -> Dict[str, Any]:
        try:
            if x is not None and y is not None:
                self.mouse.position = (x, y)
            self.mouse.press(Button.left if button == "left" else Button.right if button == "right" else Button.middle)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def mouse_up(self, x: Optional[int] = None, y: Optional[int] = None, button: str = "left") -> Dict[str, Any]:
        try:
            if x is not None and y is not None:
                self.mouse.position = (x, y)
            self.mouse.release(Button.left if button == "left" else Button.right if button == "right" else Button.middle)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def left_click(self, x: Optional[int] = None, y: Optional[int] = None) -> Dict[str, Any]:
        try:
            if x is not None and y is not None:
                self.mouse.position = (x, y)
            self.mouse.click(Button.left, 1)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def right_click(self, x: Optional[int] = None, y: Optional[int] = None) -> Dict[str, Any]:
        try:
            if x is not None and y is not None:
                self.mouse.position = (x, y)
            self.mouse.click(Button.right, 1)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def double_click(
        self, x: Optional[int] = None, y: Optional[int] = None
    ) -> Dict[str, Any]:
        try:
            if x is not None and y is not None:
                self.mouse.position = (x, y)
            self.mouse.click(Button.left, 2)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def move_cursor(self, x: int, y: int) -> Dict[str, Any]:
        try:
            self.mouse.position = (x, y)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def drag_to(
        self, x: int, y: int, button: str = "left", duration: float = 0.5
    ) -> Dict[str, Any]:
        try:
            btn = Button.left if button == "left" else Button.right if button == "right" else Button.middle
            # Press
            self.mouse.press(btn)
            # Move with sleep to simulate drag duration
            start = self.mouse.position
            steps = 20
            start_x, start_y = start
            dx = (x - start_x) / steps
            dy = (y - start_y) / steps
            for i in range(steps):
                self.mouse.position = (int(start_x + dx * (i + 1)), int(start_y + dy * (i + 1)))
                time.sleep(duration / steps)
            # Release
            self.mouse.release(btn)
            return {"success": True}
        except Exception as e:
            try:
                self.mouse.release(btn)
            except:
                pass
            return {"success": False, "error": str(e)}

    async def drag(
        self, path: List[Tuple[int, int]], button: str = "left", duration: float = 0.5
    ) -> Dict[str, Any]:
        try:
            if not path or len(path) < 2:
                return {"success": False, "error": "Path must contain at least 2 points"}
            btn = Button.left if button == "left" else Button.right if button == "right" else Button.middle
            # Move to the first point
            self.mouse.position = path[0]
            self.mouse.press(btn)
            step_duration = duration / (len(path) - 1) if len(path) > 1 else duration
            for x, y in path[1:]:
                self.mouse.position = (x, y)
                time.sleep(step_duration)
            self.mouse.release(btn)
            return {"success": True}
        except Exception as e:
            try:
                self.mouse.release(btn)
            except:
                pass
            return {"success": False, "error": str(e)}

    # Keyboard Actions
    async def key_down(self, key: str) -> Dict[str, Any]:
        try:
            # use pyautogui for their key names
            pyautogui.keyDown(key)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def key_up(self, key: str) -> Dict[str, Any]:
        try:
            # use pyautogui for their key names
            pyautogui.keyUp(key)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def type_text(self, text: str) -> Dict[str, Any]:
        try:
            # use pynput for Unicode support
            self.keyboard.type(text)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def press_key(self, key: str) -> Dict[str, Any]:
        try:
            # use pyautogui for their key names
            pyautogui.press(key)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def hotkey(self, keys: List[str]) -> Dict[str, Any]:
        try:
            # use pyautogui for their key names
            pyautogui.hotkey(*keys)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # Scrolling Actions
    async def scroll(self, x: int, y: int) -> Dict[str, Any]:
        try:
            self.mouse.scroll(x, y)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}
    
    async def scroll_down(self, clicks: int = 1) -> Dict[str, Any]:
        try:
            self.mouse.scroll(0, -clicks)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def scroll_up(self, clicks: int = 1) -> Dict[str, Any]:
        try:
            self.mouse.scroll(0, clicks)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # Screen Actions
    async def screenshot(self) -> Dict[str, Any]:
        try:
            from PIL import Image

            screenshot = pyautogui.screenshot()
            if not isinstance(screenshot, Image.Image):
                return {"success": False, "error": "Failed to capture screenshot"}

            buffered = BytesIO()
            screenshot.save(buffered, format="PNG", optimize=True)
            buffered.seek(0)
            image_data = base64.b64encode(buffered.getvalue()).decode()
            return {"success": True, "image_data": image_data}
        except Exception as e:
            return {"success": False, "error": f"Screenshot error: {str(e)}"}

    async def get_screen_size(self) -> Dict[str, Any]:
        try:
            size = pyautogui.size()
            return {"success": True, "size": {"width": size.width, "height": size.height}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def get_cursor_position(self) -> Dict[str, Any]:
        try:
            x, y = self.mouse.position
            return {"success": True, "position": {"x": x, "y": y}}
        except Exception as e:
            return {"success": False, "error": str(e)}

    # Clipboard Actions
    async def copy_to_clipboard(self) -> Dict[str, Any]:
        try:
            import pyperclip

            content = pyperclip.paste()
            return {"success": True, "content": content}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def set_clipboard(self, text: str) -> Dict[str, Any]:
        try:
            import pyperclip

            pyperclip.copy(text)
            return {"success": True}
        except Exception as e:
            return {"success": False, "error": str(e)}

    async def run_command(self, command: str) -> Dict[str, Any]:
        """Run a shell command and return its output."""
        try:
            # Create subprocess
            process = await asyncio.create_subprocess_shell(
                command,
                stdout=asyncio.subprocess.PIPE,
                stderr=asyncio.subprocess.PIPE
            )
            # Wait for the subprocess to finish
            stdout, stderr = await process.communicate()
            # Return decoded output
            return {
                "success": True, 
                "stdout": stdout.decode() if stdout else "", 
                "stderr": stderr.decode() if stderr else "",
                "return_code": process.returncode
            }
        except Exception as e:
            return {"success": False, "error": str(e)}
