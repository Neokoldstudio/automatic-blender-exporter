bl_info = {
    "name": "Heightmap Baker",
    "author": "Paul Godbert",
    "version": (1, 1, 0),
    "blender": (3, 0, 0),
    "location": "View3D > Sidebar > Tool > Heightmap Baker",
    "description": "Bake a true linear world-space heightmap from a selected mesh",
    "category": "Object",
}

import bpy
import mathutils
import os
from bpy.props import (
    IntProperty,
    FloatProperty,
    EnumProperty,
    StringProperty,
    BoolProperty,
)


# ---------------------------------------------------------------------------
# Properties
# ---------------------------------------------------------------------------

class HeightmapBakerSettings(bpy.types.PropertyGroup):

    resolution_x: IntProperty(
        name="Width",
        default=2048,
        min=64,
        max=8192,
    )

    resolution_y: IntProperty(
        name="Height",
        default=2048,
        min=64,
        max=8192,
    )

    render_engine: EnumProperty(
        name="Render Engine",
        items=[
            ('BLENDER_EEVEE', "Eevee", ""),
            ('CYCLES', "Cycles", ""),
        ],
        default='BLENDER_EEVEE',
    )

    padding: FloatProperty(
        name="Padding",
        description="Extra ortho framing around the mesh",
        default=1.0,
        min=0.01,
        soft_max=2.0,
    )

    file_format: EnumProperty(
        name="Format",
        items=[
            ('PNG', "PNG", "16-bit PNG"),
            ('OPEN_EXR', "EXR", "32-bit OpenEXR"),
        ],
        default='PNG',
    )

    bit_depth: EnumProperty(
        name="Bit Depth",
        items=[
            ('16', "16-bit", ""),
            ('32', "32-bit", ""),
        ],
        default='16'
    )

    output_dir: StringProperty(
        name="Output Directory",
        subtype='DIR_PATH',
        default=""
    )

    output_name: StringProperty(
        name="Output Name",
        default="heightmap"
    )

    invert_height: BoolProperty(
        name="Invert Height",
        default=False
    )


# ---------------------------------------------------------------------------
# Operator
# ---------------------------------------------------------------------------

class OBJECT_OT_bake_heightmap(bpy.types.Operator):
    bl_idname = "object.bake_heightmap"
    bl_label = "Bake Heightmap"
    bl_options = {'REGISTER', 'UNDO'}

    @classmethod
    def poll(cls, context):
        selected = [o for o in context.selected_objects if o.type == 'MESH']
        return len(selected) > 0

    def execute(self, context):

        scene = context.scene
        cfg = scene.heightmap_baker
        
        selected = [o for o in context.selected_objects if o.type == 'MESH']
        if not selected:
            self.report({'ERROR'}, "No mesh objects selected")
            return {'CANCELLED'}

        # ------------------------------------------------------------------
        # Store original state
        # ------------------------------------------------------------------

        original = {
            "camera": scene.camera,
            "world": scene.world,
            "engine": scene.render.engine,
            "res_x": scene.render.resolution_x,
            "res_y": scene.render.resolution_y,
            "filepath": scene.render.filepath,
            "format": scene.render.image_settings.file_format,
            "depth": scene.render.image_settings.color_depth,
            "view": scene.view_settings.view_transform,
            "look": scene.view_settings.look,
            "exposure": scene.view_settings.exposure,
            "gamma": scene.view_settings.gamma,
            "visible_objects": [o.name for o in scene.objects if o.visible_get()],
        }

        # Hide unselected objects
        for obj in scene.objects:
            if obj not in selected:
                obj.hide_set(True)
                obj.hide_render = True

        # ------------------------------------------------------------------
        # Compute WORLD-SPACE height bounds (union of all selected)
        # ------------------------------------------------------------------

        world_bb = []
        for obj in selected:
            world_bb.extend([
                obj.matrix_world @ mathutils.Vector(corner)
                for corner in obj.bound_box
            ])

        min_z = min(v.z for v in world_bb)
        max_z = max(v.z for v in world_bb)

        if max_z <= min_z:
            self.report({'ERROR'}, "Object has zero height")
            return {'CANCELLED'}

        # ------------------------------------------------------------------
        # Temporary world (background color based on invert)
        # ------------------------------------------------------------------

        temp_world = bpy.data.worlds.new("TEMP_HEIGHT_WORLD")
        temp_world.use_nodes = True

        bg = temp_world.node_tree.nodes["Background"]
        bg_value = 1.0 if cfg.invert_height else 0.0
        bg.inputs["Color"].default_value = (bg_value, bg_value, bg_value, 1)
        bg.inputs["Strength"].default_value = 1.0

        scene.world = temp_world

        # ------------------------------------------------------------------
        # Camera setup (orthographic, top-down)
        # ------------------------------------------------------------------

        min_x = min(v.x for v in world_bb)
        max_x = max(v.x for v in world_bb)
        min_y = min(v.y for v in world_bb)
        max_y = max(v.y for v in world_bb)

        size_x = max_x - min_x
        size_y = max_y - min_y

        render_ratio = cfg.resolution_x / cfg.resolution_y
        mesh_ratio = size_x / size_y

        ortho_scale = size_x if mesh_ratio > render_ratio else size_y
        ortho_scale *= cfg.padding

        center_xy = mathutils.Vector((
            (min_x + max_x) / 2,
            (min_y + max_y) / 2,
            max_z + 10.0,
        ))

        bpy.ops.object.camera_add(location=center_xy)
        cam = context.object
        cam.data.type = 'ORTHO'
        cam.data.ortho_scale = ortho_scale
        cam.rotation_euler = (0.0, 0.0, 0.0)
        scene.camera = cam

        # ------------------------------------------------------------------
        # Temporary height material (WORLD Z → 0..1)
        # ------------------------------------------------------------------

        mat = bpy.data.materials.new("TEMP_HEIGHT_MAT")
        mat.use_nodes = True
        nodes = mat.node_tree.nodes
        links = mat.node_tree.links
        nodes.clear()

        n_geo = nodes.new('ShaderNodeNewGeometry')
        n_sep = nodes.new('ShaderNodeSeparateXYZ')
        n_map = nodes.new('ShaderNodeMapRange')
        n_emit = nodes.new('ShaderNodeEmission')
        n_out = nodes.new('ShaderNodeOutputMaterial')

        n_map.inputs['From Min'].default_value = min_z
        n_map.inputs['From Max'].default_value = max_z
        if cfg.invert_height:
            n_map.inputs['To Min'].default_value = 1.0
            n_map.inputs['To Max'].default_value = 0.0
        else:
            n_map.inputs['To Min'].default_value = 0.0
            n_map.inputs['To Max'].default_value = 1.0
        n_map.clamp = True

        links.new(n_geo.outputs['Position'], n_sep.inputs[0])
        links.new(n_sep.outputs['Z'], n_map.inputs['Value'])
        links.new(n_map.outputs['Result'], n_emit.inputs['Color'])
        links.new(n_emit.outputs['Emission'], n_out.inputs['Surface'])

        # ------------------------------------------------------------------
        # Apply material to all selected objects
        # ------------------------------------------------------------------
        
        original_materials = {}
        for obj in selected:
            original_materials[obj.name] = obj.data.materials[0] if obj.data.materials else None
            if obj.data.materials:
                obj.data.materials[0] = mat
            else:
                obj.data.materials.append(mat)

        # ------------------------------------------------------------------
        # Render configuration (RAW linear output!)
        # ------------------------------------------------------------------

        scene.render.engine = cfg.render_engine
        scene.render.resolution_x = cfg.resolution_x
        scene.render.resolution_y = cfg.resolution_y
        scene.render.image_settings.file_format = cfg.file_format
        scene.render.image_settings.color_depth = cfg.bit_depth

        scene.view_settings.view_transform = 'Raw'
        scene.view_settings.look = 'None'
        scene.view_settings.exposure = 0.0
        scene.view_settings.gamma = 1.0
        scene.render.film_transparent = False

        # ------------------------------------------------------------------
        # Output path
        # ------------------------------------------------------------------

        if cfg.output_dir:
            out_dir = bpy.path.abspath(cfg.output_dir)
        elif bpy.data.is_saved:
            out_dir = bpy.path.abspath("//")
        else:
            out_dir = os.path.expanduser("~")

        ext = "exr" if cfg.file_format == 'OPEN_EXR' else "png"
        output_name = cfg.output_name if cfg.output_name else "heightmap"
        output_path = os.path.join(out_dir, f"{output_name}.{ext}")
        scene.render.filepath = output_path

        # ------------------------------------------------------------------
        # Render
        # ------------------------------------------------------------------

        bpy.ops.render.render(write_still=True)

        # ------------------------------------------------------------------
        # Cleanup and restore
        # ------------------------------------------------------------------

        for obj in selected:
            orig_mat = original_materials.get(obj.name)
            if orig_mat:
                obj.data.materials[0] = orig_mat
            else:
                obj.data.materials.clear()

        bpy.data.materials.remove(mat)
        bpy.data.worlds.remove(temp_world)
        bpy.data.objects.remove(cam, do_unlink=True)

        # Restore visibility for all objects
        for obj in scene.objects:
            if obj.name in original["visible_objects"]:
                obj.hide_set(False)
                obj.hide_render = False

        scene.camera = original["camera"]
        scene.world = original["world"]
        scene.render.engine = original["engine"]
        scene.render.resolution_x = original["res_x"]
        scene.render.resolution_y = original["res_y"]
        scene.render.filepath = original["filepath"]
        scene.render.image_settings.file_format = original["format"]
        scene.render.image_settings.color_depth = original["depth"]
        scene.view_settings.view_transform = original["view"]
        scene.view_settings.look = original["look"]
        scene.view_settings.exposure = original["exposure"]
        scene.view_settings.gamma = original["gamma"]

        self.report({'INFO'}, f"Heightmap saved: {output_path}")
        return {'FINISHED'}


# ---------------------------------------------------------------------------
# UI Panel
# ---------------------------------------------------------------------------

class VIEW3D_PT_heightmap_baker(bpy.types.Panel):
    bl_label = "Heightmap Baker"
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Tool'

    def draw(self, context):
        layout = self.layout
        cfg = context.scene.heightmap_baker

        layout.prop(cfg, "resolution_x")
        layout.prop(cfg, "resolution_y")
        layout.prop(cfg, "render_engine")
        layout.prop(cfg, "padding")
        layout.prop(cfg, "file_format", expand=True)
        layout.prop(cfg, "bit_depth", expand=True)
        layout.prop(cfg, "output_dir")
        layout.prop(cfg, "output_name")
        layout.prop(cfg, "invert_height")

        layout.separator()
        layout.label(text="Supports multiple selected objects", icon='INFO')
        layout.operator("object.bake_heightmap", icon='RENDER_STILL')

# ---------------------------------------------------------------------------
# Registration
# ---------------------------------------------------------------------------

classes = (
    HeightmapBakerSettings,
    OBJECT_OT_bake_heightmap,
    VIEW3D_PT_heightmap_baker,
)


def register():
    for c in classes:
        bpy.utils.register_class(c)
    bpy.types.Scene.heightmap_baker = bpy.props.PointerProperty(
        type=HeightmapBakerSettings
    )


def unregister():
    for c in reversed(classes):
        bpy.utils.unregister_class(c)
    del bpy.types.Scene.heightmap_baker


if __name__ == "__main__":
    register()
