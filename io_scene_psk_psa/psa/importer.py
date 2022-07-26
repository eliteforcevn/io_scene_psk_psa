import fnmatch
import os
import re
from typing import List, Optional

import bpy
import numpy as np
from bpy.props import StringProperty, BoolProperty, CollectionProperty, PointerProperty, IntProperty
from bpy.types import Operator, UIList, PropertyGroup, Panel
from bpy_extras.io_utils import ImportHelper
from mathutils import Vector, Quaternion

from .data import Psa
from .reader import PsaReader


class PsaImportOptions(object):
    def __init__(self):
        self.should_clean_keys = True
        self.should_use_fake_user = False
        self.should_stash = False
        self.sequence_names = []
        self.should_overwrite = False
        self.should_write_keyframes = True
        self.should_write_metadata = True
        self.action_name_prefix = ''


def import_psa(psa_reader: PsaReader, armature_object, options: PsaImportOptions):
    sequences = map(lambda x: psa_reader.sequences[x], options.sequence_names)
    armature_data = armature_object.data

    class ImportBone(object):
        def __init__(self, psa_bone: Psa.Bone):
            self.psa_bone: Psa.Bone = psa_bone
            self.parent: Optional[ImportBone] = None
            self.armature_bone = None
            self.pose_bone = None
            self.orig_loc: Vector = Vector()
            self.orig_quat: Quaternion = Quaternion()
            self.post_quat: Quaternion = Quaternion()
            self.fcurves = []

    def calculate_fcurve_data(import_bone: ImportBone, key_data: []):
        # Convert world-space transforms to local-space transforms.
        key_rotation = Quaternion(key_data[0:4])
        key_location = Vector(key_data[4:])
        q = import_bone.post_quat.copy()
        q.rotate(import_bone.orig_quat)
        quat = q
        q = import_bone.post_quat.copy()
        if import_bone.parent is None:
            q.rotate(key_rotation.conjugated())
        else:
            q.rotate(key_rotation)
        quat.rotate(q.conjugated())
        loc = key_location - import_bone.orig_loc
        loc.rotate(import_bone.post_quat.conjugated())
        return quat.w, quat.x, quat.y, quat.z, loc.x, loc.y, loc.z

    # Create an index mapping from bones in the PSA to bones in the target armature.
    psa_to_armature_bone_indices = {}
    armature_bone_names = [x.name for x in armature_data.bones]
    psa_bone_names = []
    for psa_bone_index, psa_bone in enumerate(psa_reader.bones):
        psa_bone_name = psa_bone.name.decode('windows-1252')
        psa_bone_names.append(psa_bone_name)
        try:
            psa_to_armature_bone_indices[psa_bone_index] = armature_bone_names.index(psa_bone_name)
        except ValueError:
            pass

    # Report if there are missing bones in the target armature.
    missing_bone_names = set(psa_bone_names).difference(set(armature_bone_names))
    if len(missing_bone_names) > 0:
        print(
            f'The armature object \'{armature_object.name}\' is missing the following bones that exist in the PSA:')
        print(list(sorted(missing_bone_names)))
    del armature_bone_names

    # Create intermediate bone data for import operations.
    import_bones = []
    import_bones_dict = dict()

    for psa_bone_index, psa_bone in enumerate(psa_reader.bones):
        bone_name = psa_bone.name.decode('windows-1252')
        if psa_bone_index not in psa_to_armature_bone_indices:  # TODO: replace with bone_name in armature_data.bones
            # PSA bone does not map to armature bone, skip it and leave an empty bone in its place.
            import_bones.append(None)
            continue
        import_bone = ImportBone(psa_bone)
        import_bone.armature_bone = armature_data.bones[bone_name]
        import_bone.pose_bone = armature_object.pose.bones[bone_name]
        import_bones_dict[bone_name] = import_bone
        import_bones.append(import_bone)

    for import_bone in filter(lambda x: x is not None, import_bones):
        armature_bone = import_bone.armature_bone
        if armature_bone.parent is not None and armature_bone.parent.name in psa_bone_names:
            import_bone.parent = import_bones_dict[armature_bone.parent.name]
        # Calculate the original location & rotation of each bone (in world-space maybe?)
        if armature_bone.get('orig_quat') is not None:
            # TODO: ideally we don't rely on bone auxiliary data like this, the non-aux data path is incorrect
            # (animations are flipped 180 around Z)
            import_bone.orig_quat = Quaternion(armature_bone['orig_quat'])
            import_bone.orig_loc = Vector(armature_bone['orig_loc'])
            import_bone.post_quat = Quaternion(armature_bone['post_quat'])
        else:
            if import_bone.parent is not None:
                import_bone.orig_loc = armature_bone.matrix_local.translation - armature_bone.parent.matrix_local.translation
                import_bone.orig_loc.rotate(armature_bone.parent.matrix_local.to_quaternion().conjugated())
                import_bone.orig_quat = armature_bone.matrix_local.to_quaternion()
                import_bone.orig_quat.rotate(armature_bone.parent.matrix_local.to_quaternion().conjugated())
                import_bone.orig_quat.conjugate()
            else:
                import_bone.orig_loc = armature_bone.matrix_local.translation.copy()
                import_bone.orig_quat = armature_bone.matrix_local.to_quaternion()
            import_bone.post_quat = import_bone.orig_quat.conjugated()

    # Create and populate the data for new sequences.
    actions = []
    for sequence in sequences:
        # Add the action.
        sequence_name = sequence.name.decode('windows-1252')
        action_name = options.action_name_prefix + sequence_name

        if options.should_overwrite and action_name in bpy.data.actions:
            action = bpy.data.actions[action_name]
        else:
            action = bpy.data.actions.new(name=action_name)

        if options.should_write_keyframes:
            # Remove existing f-curves (replace with action.fcurves.clear() in Blender 3.2)
            while len(action.fcurves) > 0:
                action.fcurves.remove(action.fcurves[-1])

            # Create f-curves for the rotation and location of each bone.
            for psa_bone_index, armature_bone_index in psa_to_armature_bone_indices.items():
                import_bone = import_bones[psa_bone_index]
                pose_bone = import_bone.pose_bone
                rotation_data_path = pose_bone.path_from_id('rotation_quaternion')
                location_data_path = pose_bone.path_from_id('location')
                import_bone.fcurves = [
                    action.fcurves.new(rotation_data_path, index=0, action_group=pose_bone.name),  # Qw
                    action.fcurves.new(rotation_data_path, index=1, action_group=pose_bone.name),  # Qx
                    action.fcurves.new(rotation_data_path, index=2, action_group=pose_bone.name),  # Qy
                    action.fcurves.new(rotation_data_path, index=3, action_group=pose_bone.name),  # Qz
                    action.fcurves.new(location_data_path, index=0, action_group=pose_bone.name),  # Lx
                    action.fcurves.new(location_data_path, index=1, action_group=pose_bone.name),  # Ly
                    action.fcurves.new(location_data_path, index=2, action_group=pose_bone.name),  # Lz
                ]

            # Read the sequence data matrix from the PSA.
            sequence_data_matrix = psa_reader.read_sequence_data_matrix(sequence_name)
            keyframe_write_matrix = np.ones(sequence_data_matrix.shape, dtype=np.int8)

            # Convert the sequence's data from world-space to local-space.
            for bone_index, import_bone in enumerate(import_bones):
                if import_bone is None:
                    continue
                for frame_index in range(sequence.frame_count):
                    # This bone has writeable keyframes for this frame.
                    key_data = sequence_data_matrix[frame_index, bone_index]
                    # Calculate the local-space key data for the bone.
                    sequence_data_matrix[frame_index, bone_index] = calculate_fcurve_data(import_bone, key_data)

            # Clean the keyframe data. This is accomplished by writing zeroes to the write matrix when there is an
            # insufficiently large change in the data from the last written frame.
            if options.should_clean_keys:
                threshold = 0.001
                for bone_index, import_bone in enumerate(import_bones):
                    if import_bone is None:
                        continue
                    for fcurve_index in range(len(import_bone.fcurves)):
                        # Get all the keyframe data for the bone's f-curve data from the sequence data matrix.
                        fcurve_frame_data = sequence_data_matrix[:, bone_index, fcurve_index]
                        last_written_datum = 0
                        for frame_index, datum in enumerate(fcurve_frame_data):
                            # If the f-curve data is not different enough to the last written frame, un-mark this data for writing.
                            if frame_index > 0 and abs(datum - last_written_datum) < threshold:
                                keyframe_write_matrix[frame_index, bone_index, fcurve_index] = 0
                            else:
                                last_written_datum = datum

            # Write the keyframes out!
            for frame_index in range(sequence.frame_count):
                for bone_index, import_bone in enumerate(import_bones):
                    if import_bone is None:
                        continue
                    bone_has_writeable_keyframes = any(keyframe_write_matrix[frame_index, bone_index])
                    if bone_has_writeable_keyframes:
                        # This bone has writeable keyframes for this frame.
                        key_data = sequence_data_matrix[frame_index, bone_index]
                        for fcurve, should_write, datum in zip(import_bone.fcurves,
                                                               keyframe_write_matrix[frame_index, bone_index],
                                                               key_data):
                            if should_write:
                                fcurve.keyframe_points.insert(frame_index, datum, options={'FAST'})

        # Write
        if options.should_write_metadata:
            action['psa_sequence_name'] = sequence_name
            action['psa_sequence_fps'] = sequence.fps

        action.use_fake_user = options.should_use_fake_user

        actions.append(action)

    # If the user specifies, store the new animations as strips on a non-contributing NLA track.
    if options.should_stash:
        if armature_object.animation_data is None:
            armature_object.animation_data_create()
        for action in actions:
            nla_track = armature_object.animation_data.nla_tracks.new()
            nla_track.name = action.name
            nla_track.mute = True
            nla_track.strips.new(name=action.name, start=0, action=action)


class PsaImportActionListItem(PropertyGroup):
    action_name: StringProperty(options=set())
    is_selected: BoolProperty(default=False, options=set())


def load_psa_file(context):
    pg = context.scene.psa_import
    pg.sequence_list.clear()
    pg.psa.bones.clear()
    pg.psa_error = ''
    try:
        # Read the file and populate the action list.
        p = os.path.abspath(pg.psa_file_path)
        psa_reader = PsaReader(p)
        for sequence in psa_reader.sequences.values():
            item = pg.sequence_list.add()
            item.action_name = sequence.name.decode('windows-1252')
        for psa_bone in psa_reader.bones:
            item = pg.psa.bones.add()
            item.bone_name = psa_bone.name.decode('windows-1252')
    except Exception as e:
        pg.psa_error = str(e)


def on_psa_file_path_updated(property, context):
    load_psa_file(context)


class PsaBonePropertyGroup(PropertyGroup):
    bone_name: StringProperty(options=set())


class PsaDataPropertyGroup(PropertyGroup):
    bones: CollectionProperty(type=PsaBonePropertyGroup)
    sequence_count: IntProperty(default=0)


class PsaImportPropertyGroup(PropertyGroup):
    psa_file_path: StringProperty(default='', options=set(), update=on_psa_file_path_updated, name='PSA File Path')
    psa_error: StringProperty(default='')
    psa: PointerProperty(type=PsaDataPropertyGroup)
    sequence_list: CollectionProperty(type=PsaImportActionListItem)
    sequence_list_index: IntProperty(name='', default=0)
    should_clean_keys: BoolProperty(default=True, name='Clean Keyframes',
                                    description='Exclude unnecessary keyframes from being written to the actions',
                                    options=set())
    should_use_fake_user: BoolProperty(default=True, name='Fake User',
                                       description='Assign each imported action a fake user so that the data block is saved even it has no users',
                                       options=set())
    should_stash: BoolProperty(default=False, name='Stash',
                               description='Stash each imported action as a strip on a new non-contributing NLA track',
                               options=set())
    should_use_action_name_prefix: BoolProperty(default=False, name='Prefix Action Name', options=set())
    action_name_prefix: StringProperty(default='', name='Prefix', options=set())
    should_overwrite: BoolProperty(default=False, name='Reuse Existing Actions', options=set(),
                                   description='If an action with a matching name already exists, the existing action will have it\'s data overwritten instead of a new action being created')
    should_write_keyframes: BoolProperty(default=True, name='Keyframes', options=set())
    should_write_metadata: BoolProperty(default=True, name='Metadata', options=set(),
                                        description='Additional data will be written to the custom properties of the Action (e.g., frame rate)')
    sequence_filter_name: StringProperty(default='', options={'TEXTEDIT_UPDATE'})
    sequence_filter_is_selected: BoolProperty(default=False, options=set(), name='Only Show Selected',
                                              description='Only show selected sequences')
    sequence_use_filter_invert: BoolProperty(default=False, options=set())
    sequence_use_filter_regex: BoolProperty(default=False, name='Regular Expression',
                                            description='Filter using regular expressions', options=set())
    select_text: PointerProperty(type=bpy.types.Text)


def filter_sequences(pg: PsaImportPropertyGroup, sequences: bpy.types.bpy_prop_collection) -> List[int]:
    bitflag_filter_item = 1 << 30
    flt_flags = [bitflag_filter_item] * len(sequences)

    if pg.sequence_filter_name is not None:
        # Filter name is non-empty.
        if pg.sequence_use_filter_regex:
            # Use regular expression. If regex pattern doesn't compile, just ignore it.
            try:
                regex = re.compile(pg.sequence_filter_name)
                for i, sequence in enumerate(sequences):
                    if not regex.match(sequence.action_name):
                        flt_flags[i] &= ~bitflag_filter_item
            except re.error:
                pass
        else:
            # User regular text matching.
            for i, sequence in enumerate(sequences):
                if not fnmatch.fnmatch(sequence.action_name, f'*{pg.sequence_filter_name}*'):
                    flt_flags[i] &= ~bitflag_filter_item

    if pg.sequence_filter_is_selected:
        for i, sequence in enumerate(sequences):
            if not sequence.is_selected:
                flt_flags[i] &= ~bitflag_filter_item

    if pg.sequence_use_filter_invert:
        # Invert filter flags for all items.
        for i, sequence in enumerate(sequences):
            flt_flags[i] ^= bitflag_filter_item

    return flt_flags


def get_visible_sequences(pg: PsaImportPropertyGroup, sequences: bpy.types.bpy_prop_collection) -> List[
    PsaImportActionListItem]:
    bitflag_filter_item = 1 << 30
    visible_sequences = []
    for i, flag in enumerate(filter_sequences(pg, sequences)):
        if bool(flag & bitflag_filter_item):
            visible_sequences.append(sequences[i])
    return visible_sequences


class PSA_UL_SequenceList(UIList):

    def draw_item(self, context, layout, data, item, icon, active_data, active_propname, index):
        row = layout.row(align=True)
        split = row.split(align=True, factor=0.75)
        column = split.row(align=True)
        column.alignment = 'LEFT'
        column.prop(item, 'is_selected', icon_only=True)
        column.label(text=item.action_name)

    def draw_filter(self, context, layout):
        pg = context.scene.psa_import
        row = layout.row()
        subrow = row.row(align=True)
        subrow.prop(pg, 'sequence_filter_name', text="")
        subrow.prop(pg, 'sequence_use_filter_invert', text="", icon='ARROW_LEFTRIGHT')
        subrow.prop(pg, 'sequence_use_filter_regex', text="", icon='SORTBYEXT')
        subrow.prop(pg, 'sequence_filter_is_selected', text="", icon='CHECKBOX_HLT')

    def filter_items(self, context, data, property):
        pg = context.scene.psa_import
        sequences = getattr(data, property)
        flt_flags = filter_sequences(pg, sequences)
        flt_neworder = bpy.types.UI_UL_list.sort_items_by_name(sequences, 'action_name')
        return flt_flags, flt_neworder


class PSA_UL_ImportSequenceList(PSA_UL_SequenceList, UIList):
    pass


class PSA_UL_ImportActionList(PSA_UL_SequenceList, UIList):
    pass


class PsaImportSequencesFromText(Operator):
    bl_idname = 'psa_import.sequences_select_from_text'
    bl_label = 'Select By Text List'
    bl_description = 'Select sequences by name from text list'
    bl_options = {'INTERNAL', 'UNDO'}

    @classmethod
    def poll(cls, context):
        pg = context.scene.psa_import
        return len(pg.sequence_list) > 0

    def invoke(self, context, event):
        return context.window_manager.invoke_props_dialog(self, width=256)

    def draw(self, context):
        layout = self.layout
        pg = context.scene.psa_import
        layout.label(icon='INFO', text='Each sequence name should be on a new line.')
        layout.prop(pg, 'select_text', text='')

    def execute(self, context):
        pg = context.scene.psa_import
        if pg.select_text is None:
            self.report({'ERROR_INVALID_CONTEXT'}, 'No text block selected')
            return {'CANCELLED'}
        contents = pg.select_text.as_string()
        count = 0
        for line in contents.split('\n'):
            for sequence in pg.sequence_list:
                if sequence.action_name == line:
                    sequence.is_selected = True
                    count += 1
        self.report({'INFO'}, f'Selected {count} sequence(s)')
        return {'FINISHED'}


class PsaImportSequencesSelectAll(Operator):
    bl_idname = 'psa_import.sequences_select_all'
    bl_label = 'All'
    bl_description = 'Select all sequences'
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        pg = context.scene.psa_import
        visible_sequences = get_visible_sequences(pg, pg.sequence_list)
        has_unselected_actions = any(map(lambda action: not action.is_selected, visible_sequences))
        return len(visible_sequences) > 0 and has_unselected_actions

    def execute(self, context):
        pg = context.scene.psa_import
        visible_sequences = get_visible_sequences(pg, pg.sequence_list)
        for sequence in visible_sequences:
            sequence.is_selected = True
        return {'FINISHED'}


class PsaImportSequencesDeselectAll(Operator):
    bl_idname = 'psa_import.sequences_deselect_all'
    bl_label = 'None'
    bl_description = 'Deselect all visible sequences'
    bl_options = {'INTERNAL'}

    @classmethod
    def poll(cls, context):
        pg = context.scene.psa_import
        visible_sequences = get_visible_sequences(pg, pg.sequence_list)
        has_selected_sequences = any(map(lambda sequence: sequence.is_selected, visible_sequences))
        return len(visible_sequences) > 0 and has_selected_sequences

    def execute(self, context):
        pg = context.scene.psa_import
        visible_sequences = get_visible_sequences(pg, pg.sequence_list)
        for sequence in visible_sequences:
            sequence.is_selected = False
        return {'FINISHED'}


class PSA_PT_ImportPanel_Advanced(Panel):
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_label = 'Advanced'
    bl_options = {'DEFAULT_CLOSED'}
    bl_parent_id = 'PSA_PT_ImportPanel'

    def draw(self, context):
        layout = self.layout
        pg = context.scene.psa_import

        col = layout.column(heading="Options")
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(pg, 'should_clean_keys')
        col.separator()
        col.prop(pg, 'should_use_fake_user')
        col.prop(pg, 'should_stash')
        col.prop(pg, 'should_use_action_name_prefix')

        if pg.should_use_action_name_prefix:
            col.prop(pg, 'action_name_prefix')


class PSA_PT_ImportPanel(Panel):
    bl_space_type = 'PROPERTIES'
    bl_region_type = 'WINDOW'
    bl_label = 'PSA Import'
    bl_context = 'data'
    bl_category = 'PSA Import'
    bl_options = {'DEFAULT_CLOSED'}

    @classmethod
    def poll(cls, context):
        return context.object.type == 'ARMATURE'

    def draw(self, context):
        layout = self.layout
        pg = context.scene.psa_import

        row = layout.row(align=True)
        row.operator(PsaImportSelectFile.bl_idname, text='', icon='FILEBROWSER')
        row.prop(pg, 'psa_file_path', text='')
        row.operator(PsaImportFileReload.bl_idname, text='', icon='FILE_REFRESH')

        if pg.psa_error != '':
            row = layout.row()
            row.label(text='File could not be read', icon='ERROR')

        box = layout.box()

        box.label(text=f'Sequences ({len(pg.sequence_list)})', icon='ARMATURE_DATA')

        # select
        rows = max(3, min(len(pg.sequence_list), 10))

        row = box.row()
        col = row.column()

        row2 = col.row(align=True)
        row2.label(text='Select')
        row2.operator(PsaImportSequencesFromText.bl_idname, text='', icon='TEXT')
        row2.operator(PsaImportSequencesSelectAll.bl_idname, text='All', icon='CHECKBOX_HLT')
        row2.operator(PsaImportSequencesDeselectAll.bl_idname, text='None', icon='CHECKBOX_DEHLT')

        col = col.row()
        col.template_list('PSA_UL_ImportSequenceList', '', pg, 'sequence_list', pg, 'sequence_list_index', rows=rows)

        col = layout.column(heading='')
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(pg, 'should_overwrite')

        col = layout.column(heading='Write')
        col.use_property_split = True
        col.use_property_decorate = False
        col.prop(pg, 'should_write_keyframes')
        col.prop(pg, 'should_write_metadata')

        selected_sequence_count = sum(map(lambda x: x.is_selected, pg.sequence_list))

        row = layout.row()

        import_button_text = 'Import'
        if selected_sequence_count > 0:
            import_button_text = f'Import ({selected_sequence_count})'

        row.operator(PsaImportOperator.bl_idname, text=import_button_text)


class PsaImportFileReload(Operator):
    bl_idname = 'psa_import.file_reload'
    bl_label = 'Refresh'
    bl_options = {'INTERNAL'}
    bl_description = 'Refresh the PSA file'

    def execute(self, context):
        load_psa_file(context)
        return {"FINISHED"}


class PsaImportSelectFile(Operator):
    bl_idname = 'psa_import.select_file'
    bl_label = 'Select'
    bl_options = {'INTERNAL'}
    bl_description = 'Select a PSA file from which to import animations'
    filepath: bpy.props.StringProperty(subtype='FILE_PATH')
    filter_glob: bpy.props.StringProperty(default="*.psa", options={'HIDDEN'})

    def execute(self, context):
        context.scene.psa_import.psa_file_path = self.filepath
        return {"FINISHED"}

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {"RUNNING_MODAL"}


class PsaImportOperator(Operator):
    bl_idname = 'psa_import.import'
    bl_label = 'Import'
    bl_description = 'Import the selected animations into the scene as actions'
    bl_options = {'INTERNAL', 'UNDO'}

    @classmethod
    def poll(cls, context):
        pg = context.scene.psa_import
        active_object = context.view_layer.objects.active
        if active_object is None or active_object.type != 'ARMATURE':
            return False
        return any(map(lambda x: x.is_selected, pg.sequence_list))

    def execute(self, context):
        pg = context.scene.psa_import
        psa_reader = PsaReader(pg.psa_file_path)
        sequence_names = [x.action_name for x in pg.sequence_list if x.is_selected]

        options = PsaImportOptions()
        options.sequence_names = sequence_names
        options.should_clean_keys = pg.should_clean_keys
        options.should_use_fake_user = pg.should_use_fake_user
        options.should_stash = pg.should_stash
        options.action_name_prefix = pg.action_name_prefix if pg.should_use_action_name_prefix else ''
        options.should_overwrite = pg.should_overwrite
        options.should_write_metadata = pg.should_write_metadata
        options.should_write_keyframes = pg.should_write_keyframes

        import_psa(psa_reader, context.view_layer.objects.active, options)

        self.report({'INFO'}, f'Imported {len(sequence_names)} action(s)')

        return {'FINISHED'}


class PsaImportFileSelectOperator(Operator, ImportHelper):
    bl_idname = 'psa_import.file_select'
    bl_label = 'File Select'
    bl_options = {'INTERNAL'}
    filename_ext = '.psa'
    filter_glob: StringProperty(default='*.psa', options={'HIDDEN'})
    filepath: StringProperty(
        name='File Path',
        description='File path used for importing the PSA file',
        maxlen=1024,
        default='')

    def invoke(self, context, event):
        context.window_manager.fileselect_add(self)
        return {'RUNNING_MODAL'}

    def execute(self, context):
        pg = context.scene.psa_import
        pg.psa_file_path = self.filepath
        return {'FINISHED'}


classes = (
    PsaImportActionListItem,
    PsaBonePropertyGroup,
    PsaDataPropertyGroup,
    PsaImportPropertyGroup,
    PSA_UL_SequenceList,
    PSA_UL_ImportSequenceList,
    PSA_UL_ImportActionList,
    PsaImportSequencesSelectAll,
    PsaImportSequencesDeselectAll,
    PsaImportSequencesFromText,
    PsaImportFileReload,
    PSA_PT_ImportPanel,
    PSA_PT_ImportPanel_Advanced,
    PsaImportOperator,
    PsaImportFileSelectOperator,
    PsaImportSelectFile,
)
