"""Helpers for an HDA button that imports Maya layout JSON into internal SOP nodes."""

import json
import os
import re

import hou


ORIENT_WRANGLE_VEX = """vector r = radians(v@rotate);\np@orient = eulertoquaternion(r, XFORM_XYZ);"""

PYTHON_SOP_CODE = '''import json

import hou

node = hou.pwd()
json_parm = node.parm("json_file")
if json_parm is None:
    raise hou.NodeError("Missing json_file parameter on this node.")

json_path = json_parm.eval().strip()
if not json_path:
    raise hou.NodeError("Set a JSON file path before cooking this node.")

try:
    with open(json_path, "r", encoding="utf-8") as input_file:
        layout = json.load(input_file)
except (OSError, ValueError) as error:
    raise hou.NodeError("Could not read Maya layout JSON: {0}".format(error))

instances = layout.get("instances")
if not isinstance(instances, list):
    raise hou.NodeError("The JSON file must contain an instances array.")

geometry = node.geometry()
geometry.clear()
asset_id_attrib = geometry.addAttrib(hou.attribType.Point, "asset_id", "")
name_attrib = geometry.addAttrib(hou.attribType.Point, "name", "")
rotate_attrib = geometry.addAttrib(hou.attribType.Point, "rotate", (0.0, 0.0, 0.0))
scale_attrib = geometry.addAttrib(hou.attribType.Point, "scale", (1.0, 1.0, 1.0))

for index, instance in enumerate(instances):
    try:
        position = tuple(float(value) for value in instance["translate"])
        rotation = tuple(float(value) for value in instance["rotate"])
        scale = tuple(float(value) for value in instance["scale"])
        asset_id = str(instance["asset_id"])
        instance_name = str(instance.get("name", "instance_{0}".format(index + 1)))
    except (KeyError, TypeError, ValueError) as error:
        raise hou.NodeError("Invalid transform data for instance {0}: {1}".format(index, error))

    if len(position) != 3 or len(rotation) != 3 or len(scale) != 3:
        raise hou.NodeError("Instance {0} must have 3-component transform values.".format(index))

    point = geometry.createPoint()
    point.setPosition(position)
    point.setAttribValue(asset_id_attrib, asset_id)
    point.setAttribValue(name_attrib, instance_name)
    point.setAttribValue(rotate_attrib, rotation)
    point.setAttribValue(scale_attrib, scale)
'''


def _require_hda_parm(node, parm_name):
    """Return an HDA parameter value or raise a clear error."""
    parm = node.parm(parm_name)
    if parm is None:
        raise hou.OperationFailed("Missing HDA parameter: {0}".format(parm_name))
    value = parm.evalAsString().strip()
    if not value:
        raise hou.OperationFailed("Set {0} on the HDA before importing.".format(parm_name))
    return value


def _resolve_json_path(node, parm_name):
    """Expand Houdini variables in the JSON path and ensure the file exists."""
    raw_path = _require_hda_parm(node, parm_name)
    expanded = hou.expandString(raw_path)
    normalized = os.path.normpath(expanded)
    if not os.path.isfile(normalized):
        raise hou.OperationFailed("JSON file not found: {0}".format(normalized))
    return normalized


def _sanitize_hda_name(raw_name):
    """Convert any string into a valid Houdini node name."""
    cleaned = re.sub(r"[^A-Za-z0-9_]", "_", str(raw_name).strip())
    if not cleaned:
        return "layout"
    if cleaned[0].isdigit():
        cleaned = "layout_" + cleaned
    return cleaned


def _layout_node_name_from_json(json_path):
    """Read first valid instance name from layout JSON for HDA renaming."""
    try:
        with open(json_path, "r", encoding="utf-8") as input_file:
            payload = json.load(input_file)
    except (OSError, ValueError) as error:
        raise hou.OperationFailed("Could not parse JSON for node rename: {0}".format(error))

    instances = payload.get("instances")
    if not isinstance(instances, list) or not instances:
        raise hou.OperationFailed("The JSON file has no instances to derive an HDA name.")

    for instance in instances:
        if isinstance(instance, dict):
            candidate = f"{instance.get('name')}_instancer"
            if candidate:
                return _sanitize_hda_name(candidate)

    raise hou.OperationFailed("No valid instance name found in JSON to rename the HDA.")


def _find_or_create_sop_parent(hda_node):
    """Find where SOP nodes should be created inside the HDA."""
    if hda_node.childTypeCategory() == hou.sopNodeTypeCategory():
        return hda_node

    for child in hda_node.children():
        if child.childTypeCategory() == hou.sopNodeTypeCategory():
            return child

    raise hou.OperationFailed(
        "No SOP network found inside this HDA. Add a SOP network or run this on a SOP-level HDA."
    )


def _ensure_json_parm_on_python_sop(python_sop):
    """Add a file-reference parm on the Python SOP if it does not exist."""
    if python_sop.parm("json_file") is not None:
        return

    parm_group = python_sop.parmTemplateGroup()
    parm_group.append(
        hou.StringParmTemplate(
            "json_file",
            "JSON File",
            1,
            string_type=hou.stringParmType.FileReference,
        )
    )
    python_sop.setParmTemplateGroup(parm_group)


def _require_point_attributes_subnet(sop_parent):
    """Return the existing point_attributes subnet used for point processing."""
    subnet = sop_parent.node("point_attributes")
    if subnet is None:
        raise hou.OperationFailed(
            "Missing required subnet 'point_attributes' inside the SOP network."
        )
    if subnet.childTypeCategory() != hou.sopNodeTypeCategory():
        raise hou.OperationFailed(
            "Node 'point_attributes' must be a SOP subnetwork."
        )
    return subnet


def build_from_hda_json(kwargs, json_parm_name="json_file"):
    """Action-button callback: rebuild internal SOP nodes from the HDA JSON parameter."""
    hda_node = kwargs.get("node")
    if hda_node is None:
        raise hou.OperationFailed("This callback requires kwargs['node'] from an HDA button.")

    json_path = _resolve_json_path(hda_node, json_parm_name)
    target_hda_name = _layout_node_name_from_json(json_path)
    hda_node.setName(target_hda_name, unique_name=True)

    sop_parent = _find_or_create_sop_parent(hda_node)
    point_attributes_subnet = _require_point_attributes_subnet(sop_parent)

    python_sop = point_attributes_subnet.node("read_maya_layout")
    if python_sop is None:
        python_sop = point_attributes_subnet.createNode("python", "read_maya_layout")
    _ensure_json_parm_on_python_sop(python_sop)
    python_sop.parm("json_file").set(json_path)
    python_sop.parm("python").set(PYTHON_SOP_CODE)

    wrangle = point_attributes_subnet.node("build_orient")
    if wrangle is None:
        wrangle = point_attributes_subnet.createNode("attribwrangle", "build_orient")
    wrangle.setInput(0, python_sop)
    wrangle.parm("snippet").set(ORIENT_WRANGLE_VEX)

    out_null = point_attributes_subnet.node("OUT_LAYOUT_POINTS")
    if out_null is None:
        out_null = point_attributes_subnet.createNode("null", "OUT_LAYOUT_POINTS")
    out_null.setInput(0, wrangle)
    out_null.setDisplayFlag(True)
    out_null.setRenderFlag(True)

    subnet_output = point_attributes_subnet.node("output0")
    if subnet_output is None:
        raise hou.OperationFailed(
            "Missing existing output node 'output0' inside point_attributes."
        )
    subnet_output.setInput(0, out_null)

    python_sop.moveToGoodPosition()
    wrangle.moveToGoodPosition()
    out_null.moveToGoodPosition()
    subnet_output.moveToGoodPosition()

    
    hou.ui.setStatusMessage(
        "Maya layout imported from: {0}".format(json_path),
        severity=hou.severityType.Message,
    )
    return out_null


def action_button_script_example():
    """Return a paste-ready action button script example for reference."""
    return (
        "import hda_action_import\n"
        "hda_action_import.on_build_pressed(kwargs, json_parm_name='json_file')\n"
    )


def on_build_pressed(kwargs, json_parm_name="json_file"):
    """Entry point for the HDA Build button callback script."""
    try:
        return build_from_hda_json(kwargs, json_parm_name=json_parm_name)
    except hou.Error as error:
        hou.ui.displayMessage(str(error), severity=hou.severityType.Error)
        return None
    except Exception as error:
        hou.ui.displayMessage(
            "Unexpected build error: {0}".format(error),
            severity=hou.severityType.Error,
        )
        return None