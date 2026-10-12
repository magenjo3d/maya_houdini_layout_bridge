"""Create and export tagged Maya geometry instances for Houdini layouts."""

import json
import os
import re
from functools import partial


_WINDOW_NAME = "mayaToHoudiniBridgeWindow"
_SOURCE_FIELD = "mayaToHoudiniBridgeSourceField"
_ASSET_ID_FIELD = "mayaToHoudiniBridgeAssetIdField"
_COUNT_FIELD = "mayaToHoudiniBridgeCountField"
_SETS_COLUMN = "mayaToHoudiniBridgeSetsColumn"
_ASSET_ID_ATTRIBUTE = "mayaToHoudiniAssetId"
_SET_CHECKBOXES = {}
_QT_DIALOG = None
_QT_SOURCE_FIELD = None
_QT_ASSET_ID_FIELD = None
_QT_COUNT_FIELD = None
_QT_SETS_LAYOUT = None
_QT_SET_CHECKBOXES = {}


def _maya_cmds():
	"""Load Maya commands or report that the script is outside Maya."""
	try:
		import maya.cmds as cmds
	except ImportError as error:
		raise RuntimeError("This tool must be run from inside Maya.") from error
	return cmds


def _qt_modules():
	"""Return Qt modules available in Maya."""
	try:
		from PySide6 import QtCore, QtWidgets
		from shiboken6 import wrapInstance
	except ImportError:
		from PySide2 import QtCore, QtWidgets
		from shiboken2 import wrapInstance
	return QtCore, QtWidgets, wrapInstance


def _main_maya_window():
	"""Return Maya main window wrapped as QWidget."""
	import maya.OpenMayaUI as omui

	QtCore, QtWidgets, wrapInstance = _qt_modules()
	main_window_ptr = omui.MQtUtil.mainWindow()
	if main_window_ptr is None:
		return None
	return wrapInstance(int(main_window_ptr), QtWidgets.QWidget)


def _ui_source_text(cmds):
	if _QT_SOURCE_FIELD is not None:
		return _QT_SOURCE_FIELD.text().strip()
	return cmds.textField(_SOURCE_FIELD, query=True, text=True).strip()


def _set_ui_source_text(cmds, value):
	if _QT_SOURCE_FIELD is not None:
		_QT_SOURCE_FIELD.setText(value)
		return
	cmds.textField(_SOURCE_FIELD, edit=True, text=value)


def _ui_asset_text(cmds):
	if _QT_ASSET_ID_FIELD is not None:
		return _QT_ASSET_ID_FIELD.text().strip()
	return cmds.textField(_ASSET_ID_FIELD, query=True, text=True).strip()


def _set_ui_asset_text(cmds, value):
	if _QT_ASSET_ID_FIELD is not None:
		_QT_ASSET_ID_FIELD.setText(value)
		return
	cmds.textField(_ASSET_ID_FIELD, edit=True, text=value)


def _ui_count_value(cmds):
	if _QT_COUNT_FIELD is not None:
		return int(_QT_COUNT_FIELD.value())
	return cmds.intField(_COUNT_FIELD, query=True, value=True)


def _selected_source_transform(cmds):
	"""Return a selected transform that can be used as an instance source."""
	selection = cmds.ls(selection=True, long=True) or []
	if not selection:
		raise ValueError("Select a transform in the scene first.")

	selected_node = selection[0]
	if cmds.nodeType(selected_node) != "transform":
		parents = cmds.listRelatives(selected_node, parent=True, fullPath=True) or []
		if not parents or cmds.nodeType(parents[0]) != "transform":
			raise ValueError("The selected object is not under a transform.")
		selected_node = parents[0]
	return selected_node


def _validate_instance_source(cmds, transform):
	"""Validate that a transform can be used as an instance source."""
	if not transform or not cmds.objExists(transform):
		raise ValueError("The source transform does not exist.")
	if cmds.nodeType(transform) != "transform":
		raise ValueError("The source must be a transform.")

	has_shapes = bool(
		cmds.listRelatives(transform, shapes=True, noIntermediate=True, fullPath=True) or []
	)
	has_transform_children = bool(
		cmds.listRelatives(transform, children=True, type="transform", fullPath=True) or []
	)
	if not has_shapes and not has_transform_children:
		raise ValueError(
			"The source transform is empty. Select a transform with geometry or child transforms."
		)


def _show_error(message):
	"""Display an error dialog in Maya."""
	_maya_cmds().confirmDialog(
		title="Maya to Houdini Bridge",
		message=message,
		button=["OK"],
		defaultButton="OK",
		icon="critical",
	)


def _use_selection(*_):
	"""Copy the selected geometry and its name into the UI fields."""
	cmds = _maya_cmds()
	try:
		source = _selected_source_transform(cmds)
		_validate_instance_source(cmds, source)
	except ValueError as error:
		_show_error(str(error))
		return

	asset_id = source.rsplit("|", 1)[-1].rsplit(":", 1)[-1]
	_set_ui_source_text(cmds, source)
	_set_ui_asset_text(cmds, asset_id)


def _make_node_prefix(asset_id):
	"""Convert an asset ID into a valid Maya node-name prefix."""
	prefix = re.sub(r"[^A-Za-z0-9_]", "_", asset_id.strip())
	if not prefix:
		raise ValueError("Enter an asset ID before creating instances.")
	if not re.match(r"[A-Za-z_]", prefix):
		prefix = "asset_" + prefix
	return prefix


def _existing_instance_serials(cmds, group_path, prefix):
	"""Return all numeric suffixes already used by instances in the group."""
	serials = set()
	children = cmds.listRelatives(group_path, children=True, type="transform", fullPath=False) or []
	prefix_token = prefix + "_"
	for child in children:
		if child.startswith(prefix_token):
			suffix = child[len(prefix_token):]
			if suffix.isdigit():
				serials.add(int(suffix))
	return serials


def _instance_set_name(prefix):
	"""Return the set name used to track exported instances per asset."""
	return prefix + "_instances_export_set"


def _instance_sets(cmds):
	"""Return all bridge instance sets in the scene."""
	return cmds.ls("*_instances_export_set", type="objectSet", long=True) or []


def _refresh_set_checkboxes(cmds, force_checked_sets=None):
	"""Rebuild the export set checkboxes from current scene sets."""
	global _SET_CHECKBOXES, _QT_SET_CHECKBOXES
	if _QT_SETS_LAYOUT is None:
		if not cmds.columnLayout(_SETS_COLUMN, exists=True):
			return

		force_checked_sets = set(force_checked_sets or [])
		previous_values = {}
		for set_path, checkbox in _SET_CHECKBOXES.items():
			if cmds.control(checkbox, exists=True):
				previous_values[set_path] = cmds.checkBox(checkbox, query=True, value=True)

		children = cmds.columnLayout(_SETS_COLUMN, query=True, childArray=True) or []
		for child in children:
			cmds.deleteUI(child)

		_SET_CHECKBOXES = {}
		for set_path in _instance_sets(cmds):
			short_name = set_path.rsplit("|", 1)[-1]
			checked = True
			if set_path in previous_values:
				checked = previous_values[set_path]
			if set_path in force_checked_sets:
				checked = True
			checkbox = cmds.checkBox(label=short_name, value=checked, parent=_SETS_COLUMN)
			_SET_CHECKBOXES[set_path] = checkbox

		if not _SET_CHECKBOXES:
			cmds.text(label="No instance sets found.", align="left", parent=_SETS_COLUMN)
		return

	force_checked_sets = set(force_checked_sets or [])
	previous_values = {
		set_path: checkbox.isChecked()
		for set_path, checkbox in _QT_SET_CHECKBOXES.items()
		if checkbox is not None
	}
	while _QT_SETS_LAYOUT.count():
		item = _QT_SETS_LAYOUT.takeAt(0)
		widget = item.widget()
		if widget is not None:
			widget.deleteLater()

	_QT_SET_CHECKBOXES = {}
	QtCore, QtWidgets, _wrap = _qt_modules()
	for set_path in _instance_sets(cmds):
		short_name = set_path.rsplit("|", 1)[-1]
		checked = True
		if set_path in previous_values:
			checked = previous_values[set_path]
		if set_path in force_checked_sets:
			checked = True
		checkbox = QtWidgets.QCheckBox(short_name)
		checkbox.setChecked(checked)
		_QT_SETS_LAYOUT.addWidget(checkbox)
		_QT_SET_CHECKBOXES[set_path] = checkbox

	if not _QT_SET_CHECKBOXES:
		label = QtWidgets.QLabel("No instance sets found.")
		_QT_SETS_LAYOUT.addWidget(label)


def _checked_export_sets(cmds):
	"""Return currently checked instance sets from the UI list."""
	if _QT_SET_CHECKBOXES:
		target_sets = [
			set_path for set_path, checkbox in _QT_SET_CHECKBOXES.items()
			if checkbox is not None and checkbox.isChecked() and cmds.objExists(set_path)
		]
		if not target_sets:
			raise ValueError("Check at least one set in Export Sets before exporting.")
		return target_sets

	if not _SET_CHECKBOXES:
		raise ValueError("No bridge instance sets found. Create instances before exporting.")

	target_sets = []
	for set_path, checkbox in _SET_CHECKBOXES.items():
		if not cmds.control(checkbox, exists=True):
			continue
		if not cmds.objExists(set_path):
			continue
		if cmds.checkBox(checkbox, query=True, value=True):
			target_sets.append(set_path)

	if not target_sets:
		raise ValueError("Check at least one set in Export Sets before exporting.")
	return target_sets


def _create_instances(*_):
	"""Create, group, color, and tag the requested geometry instances."""
	cmds = _maya_cmds()
	source = _ui_source_text(cmds)
	asset_id = _ui_asset_text(cmds)
	count = _ui_count_value(cmds)

	try:
		if not source or not cmds.objExists(source):
			source = _selected_source_transform(cmds)
			_set_ui_source_text(cmds, source)
		_validate_instance_source(cmds, source)
		if not asset_id:
			asset_id = source.rsplit("|", 1)[-1].rsplit(":", 1)[-1]
			_set_ui_asset_text(cmds, asset_id)
		prefix = _make_node_prefix(asset_id)
	except ValueError as error:
		_show_error(str(error))
		return

	group_name = prefix + "_instances_grp"
	set_name = _instance_set_name(prefix)
	created_new_set = False
	created = []
	refresh_suspended = False
	undo_chunk_open = False
	try:
		if cmds.objExists(group_name):
			groups = cmds.ls(group_name, type="transform", long=True) or []
			if not groups:
				raise ValueError("An object named {0} exists but is not a transform group.".format(group_name))
			instance_group = groups[0]
		else:
			instance_group = cmds.group(empty=True, name=group_name)
		cmds.setAttr(instance_group + ".useOutlinerColor", 1)
		cmds.setAttr(instance_group + ".outlinerColor", 0.0, 0.5, 0.0, type="double3")

		existing_serials = _existing_instance_serials(cmds, instance_group, prefix)
		serial = max(existing_serials) + 1 if existing_serials else 1

		if cmds.objExists(set_name):
			if cmds.nodeType(set_name) != "objectSet":
				raise ValueError("An object named {0} exists but is not a set.".format(set_name))
			instance_set = set_name
		else:
			instance_set = cmds.sets(name=set_name, empty=True)
			created_new_set = True

		cmds.undoInfo(openChunk=True, chunkName="Create Maya Bridge Instances")
		undo_chunk_open = True
		cmds.refresh(suspend=True)
		refresh_suspended = True

		for _index in range(count):
			while serial in existing_serials:
				serial += 1
			instance_name = "{0}_{1}".format(prefix, serial)
			existing_serials.add(serial)
			serial += 1
			instance_nodes = cmds.instance(source, name=instance_name)
			instance_transform = cmds.parent(instance_nodes[0], instance_group)[0]
			cmds.setAttr(instance_transform + ".useOutlinerColor", 1)
			cmds.setAttr(instance_transform + ".outlinerColor", 0.0, 1.0, 0.0, type="double3")
			if not cmds.attributeQuery(_ASSET_ID_ATTRIBUTE, node=instance_transform, exists=True):
				cmds.addAttr(instance_transform, longName=_ASSET_ID_ATTRIBUTE, dataType="string")
			cmds.setAttr(
				instance_transform + "." + _ASSET_ID_ATTRIBUTE,
				asset_id,
				type="string",
			)
			created.append(instance_transform)

		if created:
			cmds.sets(created, add=instance_set)
	except Exception as error:
		_show_error("Could not create instances: {0}".format(error))
		return
	finally:
		if refresh_suspended:
			cmds.refresh(suspend=False)
			cmds.refresh(force=True)
		if undo_chunk_open:
			cmds.undoInfo(closeChunk=True)

	if created:
		_refresh_set_checkboxes(
			cmds,
			force_checked_sets=[set_name] if created_new_set else None,
		)
		cmds.inViewMessage(
			message="Created {0} instance(s) for {1}.".format(len(created), asset_id),
			position="midCenter",
			fade=True,
		)


def _set_member_transforms(cmds, member):
	"""Return transform members for a set entry, including transform descendants."""
	transforms = set()
	transform_nodes = cmds.ls(member, long=True, type="transform") or []
	for transform in transform_nodes:
		transforms.add(transform)
		descendants = cmds.listRelatives(transform, allDescendents=True, type="transform", fullPath=True) or []
		for descendant in descendants:
			transforms.add(descendant)

	if transforms:
		return list(transforms)

	shape_nodes = cmds.ls(member, long=True, shapes=True) or []
	for shape in shape_nodes:
		parents = cmds.listRelatives(shape, parent=True, type="transform", fullPath=True) or []
		for parent in parents:
			transforms.add(parent)
	return list(transforms)


def _tagged_instances(cmds, instance_sets):
	"""Collect transforms under the provided sets, tagged or untagged."""
	tagged = []
	seen = set()
	for instance_set in instance_sets:
		members = cmds.sets(instance_set, query=True) or []
		for member in members:
			transform_nodes = _set_member_transforms(cmds, member)
			for transform in transform_nodes:
				if transform in seen:
					continue
				asset_id = ""
				if cmds.attributeQuery(_ASSET_ID_ATTRIBUTE, node=transform, exists=True):
					asset_id = cmds.getAttr(transform + "." + _ASSET_ID_ATTRIBUTE) or ""
				if not asset_id:
					asset_id = transform.rsplit("|", 1)[-1]
				seen.add(transform)
				tagged.append((transform, asset_id))
	return tagged


def _export_json(*_):
	"""Write one JSON file per checked export set into a chosen folder."""
	cmds = _maya_cmds()
	_refresh_set_checkboxes(cmds)
	try:
		target_sets = _checked_export_sets(cmds)
	except ValueError as error:
		_show_error(str(error))
		return

	selected_folders = cmds.fileDialog2(
		fileMode=3,
		caption="Choose export folder",
		dialogStyle=2,
	) or []
	if not selected_folders:
		return

	output_folder = selected_folders[0]
	exported_files = []
	for instance_set in target_sets:
		instances = _tagged_instances(cmds, [instance_set])
		if not instances:
			continue

		export_data = {
			"schema_version": 1,
			"source_scene": cmds.file(query=True, sceneName=True),
			"instances": [],
		}
		for transform, asset_id in instances:
			export_data["instances"].append(
				{
					"name": asset_id,
					"asset_id": transform.rsplit("|", 1)[-1],
					"translate": cmds.xform(transform, query=True, worldSpace=True, translation=True),
					"rotate": cmds.xform(transform, query=True, worldSpace=True, rotation=True),
					"scale": cmds.xform(transform, query=True, worldSpace=True, scale=True),
				}
			)

		set_short_name = instance_set.rsplit("|", 1)[-1]
		output_path = os.path.join(output_folder, set_short_name + ".json")
		try:
			with open(output_path, "w", encoding="utf-8") as output_file:
				json.dump(export_data, output_file, indent=2, ensure_ascii=False)
		except (OSError, TypeError, ValueError) as error:
			_show_error("Could not write JSON for set {0}: {1}".format(set_short_name, error))
			return

		exported_files.append(os.path.normpath(output_path))

	if not exported_files:
		_show_error("No bridge instances found in the checked set(s).")
		return

	cmds.confirmDialog(
		title="Maya to Houdini Bridge",
		message="Exported {0} set file(s) to:\n{1}".format(
			len(exported_files),
			os.path.normpath(output_folder),
		),
		button=["OK"],
		defaultButton="OK",
		icon="information",
	)


def show_ui():
	"""Build and display the Maya bridge window."""
	global _SET_CHECKBOXES, _QT_DIALOG, _QT_SOURCE_FIELD, _QT_ASSET_ID_FIELD, _QT_COUNT_FIELD, _QT_SETS_LAYOUT, _QT_SET_CHECKBOXES
	cmds = _maya_cmds()
	QtCore, QtWidgets, _wrap = _qt_modules()
	if _QT_DIALOG is not None:
		_QT_DIALOG.close()
		_QT_DIALOG.deleteLater()
		_QT_DIALOG = None
	_SET_CHECKBOXES = {}
	_QT_SET_CHECKBOXES = {}

	dialog = QtWidgets.QDialog(_main_maya_window())
	dialog.setObjectName(_WINDOW_NAME)
	dialog.setWindowTitle("Maya to Houdini Bridge")
	dialog.setWindowFlags(dialog.windowFlags() & ~QtCore.Qt.WindowContextHelpButtonHint)
	dialog.setMinimumSize(500, 390)
	dialog.setMaximumSize(500, 390)

	grid = QtWidgets.QGridLayout(dialog)
	grid.setContentsMargins(10, 10, 10, 10)
	grid.setHorizontalSpacing(8)
	grid.setVerticalSpacing(8)

	grid.addWidget(QtWidgets.QLabel("Instance Asset"), 0, 0, 1, 3)

	_QT_SOURCE_FIELD = QtWidgets.QLineEdit()
	_QT_SOURCE_FIELD.setPlaceholderText("Select a geometry transform")
	grid.addWidget(_QT_SOURCE_FIELD, 1, 0, 1, 2)

	use_button = QtWidgets.QPushButton("Use Selection")
	use_button.clicked.connect(partial(_use_selection))
	grid.addWidget(use_button, 1, 2)

	_QT_ASSET_ID_FIELD = QtWidgets.QLineEdit()
	_QT_ASSET_ID_FIELD.setPlaceholderText("Asset ID")
	grid.addWidget(_QT_ASSET_ID_FIELD, 2, 0, 1, 2)

	_QT_COUNT_FIELD = QtWidgets.QSpinBox()
	_QT_COUNT_FIELD.setRange(1, 999)
	_QT_COUNT_FIELD.setValue(1)
	grid.addWidget(_QT_COUNT_FIELD, 2, 2)

	create_button = QtWidgets.QPushButton("Create Instances")
	create_button.setMinimumHeight(30)
	create_button.clicked.connect(partial(_create_instances))
	grid.addWidget(create_button, 3, 0, 1, 3)

	grid.addWidget(QtWidgets.QLabel("Export Sets"), 4, 0, 1, 3)

	scroll_area = QtWidgets.QScrollArea()
	scroll_area.setWidgetResizable(True)
	scroll_area.setMinimumHeight(125)
	scroll_widget = QtWidgets.QWidget()
	_QT_SETS_LAYOUT = QtWidgets.QVBoxLayout(scroll_widget)
	_QT_SETS_LAYOUT.setContentsMargins(4, 4, 4, 4)
	_QT_SETS_LAYOUT.setSpacing(4)
	scroll_area.setWidget(scroll_widget)
	grid.addWidget(scroll_area, 5, 0, 1, 3)

	refresh_button = QtWidgets.QPushButton("Refresh Sets")
	refresh_button.clicked.connect(lambda *_args: _refresh_set_checkboxes(_maya_cmds()))
	grid.addWidget(refresh_button, 6, 0, 1, 3)

	export_button = QtWidgets.QPushButton("Export Layout JSON...")
	export_button.setMinimumHeight(30)
	export_button.clicked.connect(partial(_export_json))
	grid.addWidget(export_button, 7, 0, 1, 3)

	_QT_DIALOG = dialog
	_refresh_set_checkboxes(cmds)
	dialog.show()


if __name__ == "__main__":
	show_ui()
