# -*- coding: utf-8 -*-
# ================================================================
#  Native Mesh Reducer (纯净稳健版 + 射线防穿插删内部面 + UI完美贴边重构版)
#  特性: 保留增强版所有算法(对称/Shell定位)，采用 xiaotaTool 的 Workspace 贴边 UI
# ================================================================

import maya.cmds as cmds
import maya.api.OpenMaya as om
import math
import re

WINDOW_ID = "SGMayaNativeReducerWin"
WIDGETS   = {}

PREVIEW_NODE    = None
PREVIEW_NODE_ID = None
PREVIEW_TARGETS = []

# 缓存上次分析的低密度 Shell 面列表，供"选中所有低密度"按钮使用
LAST_LOW_DENSITY_FACES = []
LAST_ANALYSIS_MESH     = None

# ════════════════════════════════════════════════════════════════
#  [核心辅助] 智能对象获取器 (兼容物体级别和点线面级别)
# ════════════════════════════════════════════════════════════════
def get_selected_transforms():
    """智能获取当前操作的模型变换节点，兼容物体级别和点线面级别"""
    raw_objs = set(cmds.ls(selection=True, objectsOnly=True, long=True) or [])
    hilite_objs = set(cmds.ls(hilite=True, long=True) or [])
    combined = list(raw_objs.union(hilite_objs))

    transforms = set()
    for obj in combined:
        if cmds.nodeType(obj) == 'transform':
            if cmds.listRelatives(obj, shapes=True, type='mesh', fullPath=True):
                transforms.add(obj)
        elif cmds.nodeType(obj) == 'mesh':
            parent = cmds.listRelatives(obj, parent=True, type='transform', fullPath=True)
            if parent:
                transforms.add(parent[0])
                
    return list(transforms)


# ════════════════════════════════════════════════════════════════
#  [核心] 内部穿插面极速清理 (UV阈值 + 空间射线检测 + 法线感知魔法)
# ════════════════════════════════════════════════════════════════

def delete_internal_shells(mesh, uv_threshold, log_fn):
    """基于 texel density (均匀密度基准) + 射线遮挡 判定内部面"""
    global LAST_LOW_DENSITY_FACES, LAST_ANALYSIS_MESH
    LAST_LOW_DENSITY_FACES = []
    LAST_ANALYSIS_MESH = mesh
    try:
        sel = om.MSelectionList()
        sel.add(mesh)
        try:
            dag = sel.getDagPath(0)
        except TypeError:
            dag = om.MDagPath()
            sel.getDagPath(0, dag)
        mfn = om.MFnMesh(dag)

        accel = mfn.autoUniformGridParams()
        uv_set = mfn.currentUVSetName()

        try:
            nbUvShells, uvShellIds = mfn.getUvShellsIds(uv_set)
        except Exception:
            log_fn("  !! 获取 UV Shell 失败，模型可能未展开 UV。")
            return

        try:
            all_u, all_v = mfn.getUVs(uv_set)
        except Exception:
            log_fn("  !! getUVs 失败，模型可能没有 UV 数据。")
            return

        if len(all_u) == 0:
            log_fn("  !! UV 坐标数组为空，跳过内部面清理。")
            return

        global_area_3d = 0.0
        global_area_uv = 0.0

        shell_data = {
            i: {'faces': [], 'area_3d': 0.0, 'area_uv': 0.0, 'centers': [], 'normals': []}
            for i in range(nbUvShells)
        }

        points = mfn.getPoints(om.MSpace.kWorld)
        num_polys = mfn.numPolygons

        for i in range(num_polys):
            v_idx = mfn.getPolygonVertices(i)
            area_3d = 0.0
            if len(v_idx) >= 3:
                p0 = points[v_idx[0]]
                for j in range(1, len(v_idx) - 1):
                    p1, p2 = points[v_idx[j]], points[v_idx[j+1]]
                    v1x, v1y, v1z = p1.x - p0.x, p1.y - p0.y, p1.z - p0.z
                    v2x, v2y, v2z = p2.x - p0.x, p2.y - p0.y, p2.z - p0.z
                    cx = v1y * v2z - v1z * v2y
                    cy = v1z * v2x - v1x * v2z
                    cz = v1x * v2y - v1y * v2x
                    area_3d += 0.5 * math.sqrt(cx*cx + cy*cy + cz*cz)

            if area_3d < 1e-8:
                continue

            area_uv = 0.0
            n_verts = len(v_idx)
            try:
                face_us = []
                face_vs = []
                for fv in range(n_verts):
                    uv_id = mfn.getPolygonUVid(i, fv, uv_set)
                    face_us.append(all_u[uv_id])
                    face_vs.append(all_v[uv_id])

                if len(face_us) >= 3:
                    cross_sum = 0.0
                    for j in range(len(face_us)):
                        nxt = (j + 1) % len(face_us)
                        cross_sum += face_us[j] * face_vs[nxt] - face_us[nxt] * face_vs[j]
                    area_uv = abs(cross_sum) * 0.5
            except Exception:
                area_uv = 0.0

            try:
                first_uv_id = mfn.getPolygonUVid(i, 0, uv_set)
                shell_id = uvShellIds[first_uv_id]

                shell_data[shell_id]['faces'].append(i)
                shell_data[shell_id]['area_3d'] += area_3d
                shell_data[shell_id]['area_uv'] += area_uv

                global_area_3d += area_3d
                global_area_uv += area_uv

                cx_pos = cy_pos = cz_pos = 0.0
                for v in v_idx:
                    p = points[v]
                    cx_pos += p.x; cy_pos += p.y; cz_pos += p.z
                center = om.MFloatPoint(cx_pos/len(v_idx), cy_pos/len(v_idx), cz_pos/len(v_idx))
                normal = mfn.getPolygonNormal(i, om.MSpace.kWorld)

                shell_data[shell_id]['centers'].append(center)
                shell_data[shell_id]['normals'].append(normal)
            except Exception:
                pass

        if global_area_3d < 1e-6:
            return

        shell_densities = []
        for sid, sdata in shell_data.items():
            if sdata['area_3d'] > 1e-6 and sdata['faces']:
                shell_densities.append(sdata['area_uv'] / sdata['area_3d'])

        if not shell_densities:
            log_fn("  !! 没有有效的 UV Shell，跳过。")
            return

        shell_densities.sort()
        n_shells = len(shell_densities)
        if n_shells % 2 == 1:
            median_density = shell_densities[n_shells // 2]
        else:
            median_density = (shell_densities[n_shells // 2 - 1] + shell_densities[n_shells // 2]) / 2.0

        if median_density < 1e-10:
            log_fn("  !! 中位数密度接近 0，模型可能未展 UV，跳过。")
            return

        faces_to_delete = []
        deleted_shells = 0

        for shell_id, data in shell_data.items():
            if data['area_3d'] < 1e-6 or not data['faces']:
                continue

            shell_density = data['area_uv'] / data['area_3d']
            total_faces = len(data['faces'])
            density_ratio = shell_density / median_density if median_density > 0 else 0
            uv_is_low = shell_density < (median_density * uv_threshold)

            mark = "*" if uv_is_low else " "
            low_txt = " 低密度" if uv_is_low else ""
            log_fn(" {} [Shell {:>2}]  面数:{:<4}  比值:{:>6.1f}%{}".format(
                mark, shell_id, total_faces, density_ratio * 100, low_txt))

            if not uv_is_low:
                continue

            for f in data['faces']:
                LAST_LOW_DENSITY_FACES.append("{}.f[{}]".format(mesh, f))

            shell_face_set = set(data['faces'])
            hit_count = 0

            for idx in range(total_faces):
                center = data['centers'][idx]
                normal = data['normals'][idx]

                ray_dir = om.MFloatVector(normal.x, normal.y, normal.z)
                origin = om.MFloatPoint(center.x + ray_dir.x * 0.01,
                                        center.y + ray_dir.y * 0.01,
                                        center.z + ray_dir.z * 0.01)
                try:
                    hit = mfn.closestIntersection(origin, ray_dir, om.MSpace.kWorld, 9999.0, False, accelParams=accel)
                    if hit and hit[2] != -1:
                        hit_face_idx = hit[2]
                        if hit_face_idx not in shell_face_set:
                            hit_normal = mfn.getPolygonNormal(hit_face_idx, om.MSpace.kWorld)
                            dot_val = ray_dir.x * hit_normal.x + ray_dir.y * hit_normal.y + ray_dir.z * hit_normal.z
                            if dot_val > 0.0:
                                hit_count += 1
                except Exception:
                    pass

            hit_ratio = hit_count / float(total_faces) if total_faces > 0 else 0.0
            if hit_ratio >= 0.8:
                log_fn(" X [Shell {:>2}]  射线:{:>3.0f}%  >> 删除 <<".format(shell_id, hit_ratio * 100))
                deleted_shells += 1
                for f in data['faces']:
                    faces_to_delete.append("{}.f[{}]".format(mesh, f))

        if faces_to_delete:
            cmds.delete(faces_to_delete)
            log_fn("  [清理] 成功剔除 {} 个低密度内部 Shell (共 {:,} 面)".format(deleted_shells, len(faces_to_delete)))
            try:
                shapes = cmds.listRelatives(mesh, shapes=True, fullPath=True)
                if shapes:
                    cmds.setAttr(shapes[0] + ".displayCenter", 0)
            except Exception:
                pass
        else:
            log_fn("  [清理] 未发现符合阈值的隐藏 UV 块")

    except Exception as e:
        log_fn("  !! 内部块清理算法报错: {}".format(e))


def set_viewport_highlight(state):
    try:
        for panel in cmds.getPanel(type='modelPanel'):
            cmds.modelEditor(panel, edit=True, selectionHiliteDisplay=state)
    except Exception:
        pass

def on_ui_close():
    set_viewport_highlight(True)
    if PREVIEW_TARGETS:
        try:
            cmds.select(PREVIEW_TARGETS, replace=True)
        except Exception:
            pass

def get_skin_cluster(mesh):
    for node in (cmds.listHistory(mesh, pruneDagObjects=True) or []):
        if cmds.nodeType(node) == 'skinCluster':
            return node
    return None

def rebind_skin_pre_reduce(src_mesh, src_sc, dst_mesh, log_fn):
    infs = cmds.skinCluster(src_sc, q=True, influence=True) or []
    if not infs: return False
    try:
        max_inf     = cmds.skinCluster(src_sc, q=True, maximumInfluences=True)
        skin_method = cmds.skinCluster(src_sc, q=True, skinMethod=True)
        cmds.select(infs, replace=True)
        cmds.select(dst_mesh, add=True)
        new_sc = cmds.skinCluster(
            toSelectedBones=True, maximumInfluences=max_inf,
            skinMethod=skin_method, normalizeWeights=1, obeyMaxInfluences=True
        )[0]
        cmds.copySkinWeights(
            sourceSkin=src_sc, destinationSkin=new_sc, noMirror=True,
            surfaceAssociation='closestPoint', influenceAssociation=['name', 'closestJoint'], normalize=True
        )
        cmds.select(clear=True)
        return True
    except Exception as e:
        return False

def do_reduce(mesh, use_tri_count, keep_pct, target_tris, kwargs_reduce,
              protect_skin, triangulate, do_del_internal, uv_thresh, log_fn):
    if not cmds.objExists(mesh): return False
    skin     = get_skin_cluster(mesh)
    orig_tri = cmds.polyEvaluate(mesh, triangle=True)
    log_fn("\n" + "━" * 26)
    log_fn("  网格: {} | {:,} tri".format(mesh, orig_tri))

    absolute_target_tris = target_tris if use_tri_count else int(orig_tri * (keep_pct / 100.0))
    base = mesh.split('|')[-1].split(':')[-1]
    dup  = cmds.duplicate(mesh, returnRootsOnly=True)[0]
    dup  = cmds.rename(dup, "{}_LOD".format(base))
    cmds.delete(dup, constructionHistory=True)

    is_skinned_lod = False
    if skin and protect_skin:
        is_skinned_lod = rebind_skin_pre_reduce(mesh, skin, dup, log_fn)

    if do_del_internal:
        delete_internal_shells(dup, uv_thresh, log_fn)

    current_tri = cmds.polyEvaluate(dup, triangle=True)
    if current_tri <= absolute_target_tris:
        log_fn("  [Reduce] 内部清理已达标 (剩余 {:,} tri ≤ 目标 {:,} tri)，跳过引擎减面。".format(current_tri, absolute_target_tris))
        if triangulate: cmds.polyTriangulate(dup, constructionHistory=False)
    else:
        reduce_args = {"replaceOriginal": True, "cachingReduce": True, "constructionHistory": True}
        reduce_args.update(kwargs_reduce)
        reduce_args["termination"]   = 2
        reduce_args["triangleCount"] = absolute_target_tris
        try:
            cmds.polyReduce(dup, **reduce_args)
            if triangulate: cmds.polyTriangulate(dup, constructionHistory=False)
        except Exception as e:
            log_fn("  !! 减面失败: {}".format(e))
            cmds.delete(dup)
            return False

    if is_skinned_lod:
        cmds.bakePartialHistory(dup, prePostDeformers=True)
    else:
        cmds.delete(dup, constructionHistory=True)

    final_tri  = cmds.polyEvaluate(dup, triangle=True)
    log_fn("  结果: {:,} tri (最终保留原始的 {:.1f}%)".format(final_tri, (final_tri / float(orig_tri)) * 100.0))
    return True

def is_preview_node_valid():
    global PREVIEW_NODE, PREVIEW_NODE_ID
    if not PREVIEW_NODE or not PREVIEW_NODE_ID or not cmds.objExists(PREVIEW_NODE): return False
    cur_id = cmds.ls(PREVIEW_NODE, uuid=True)
    if not cur_id or cur_id[0] != PREVIEW_NODE_ID:
        PREVIEW_NODE = PREVIEW_NODE_ID = None
        return False
    return True

def clear_stale_history(targets):
    try:
        transforms = cmds.ls(targets, objectsOnly=True)
        if transforms:
            stale = [n for n in (cmds.listHistory(transforms) or []) if cmds.nodeType(n) == 'polySoftEdge']
            if stale: cmds.delete(stale)
    except Exception: pass

def preview_soft_edge(*args):
    global PREVIEW_NODE, PREVIEW_NODE_ID, PREVIEW_TARGETS
    val = cmds.intSliderGrp(WIDGETS['hard_angle'], q=True, value=True)
    current_sel = cmds.ls(selection=True)

    if current_sel:
        if current_sel != PREVIEW_TARGETS:
            if PREVIEW_TARGETS: clear_stale_history(PREVIEW_TARGETS)
            PREVIEW_NODE = PREVIEW_NODE_ID = None
        PREVIEW_TARGETS = current_sel

    if not PREVIEW_TARGETS: return

    if is_preview_node_valid():
        try:
            cmds.setAttr(PREVIEW_NODE + ".angle", val)
            cmds.select(clear=True)
            cmds.refresh()
            return
        except Exception:
            PREVIEW_NODE = PREVIEW_NODE_ID = None

    edges = cmds.filterExpand(cmds.polyListComponentConversion(PREVIEW_TARGETS, toEdge=True), selectionMask=32)
    if not edges: return

    try: cmds.polyNormalPerVertex(cmds.polyListComponentConversion(PREVIEW_TARGETS, toVertex=True), unFreezeNormal=True)
    except Exception: pass

    res = cmds.polySoftEdge(edges, angle=val, constructionHistory=True)
    if res:
        PREVIEW_NODE = res[0]
        PREVIEW_NODE_ID = cmds.ls(PREVIEW_NODE, uuid=True)[0]

    cmds.select(clear=True)
    cmds.refresh()

def cancel_soft_edge(*args):
    global PREVIEW_NODE, PREVIEW_NODE_ID, PREVIEW_TARGETS
    targets = list(PREVIEW_TARGETS)
    if not targets:
        log_append("  !! 没有需要还原的预览。")
        return
        
    cmds.undoInfo(openChunk=True)
    try:
        trans = cmds.ls(targets, objectsOnly=True)
        if trans:
            for t in trans:
                hist = cmds.listHistory(t) or []
                nodes_to_delete = []
                for n in hist:
                    ntype = cmds.nodeType(n)
                    if ntype in ['mesh', 'transform', 'groupParts', 'groupId']: continue
                    if ntype in ['polySoftEdge', 'polyNormalPerVertex', 'polyNormal']: nodes_to_delete.append(n)
                    else: break
                if nodes_to_delete: cmds.delete(nodes_to_delete)
        log_append("  [后期修复] 已取消，法线完美还原！")
    except Exception as e:
        log_append("  !! 还原失败: {}".format(e))
    finally:
        PREVIEW_NODE = PREVIEW_NODE_ID = None
        PREVIEW_TARGETS = []
        cmds.undoInfo(closeChunk=True)
        set_viewport_highlight(True)
        cmds.select(clear=True)

def apply_soft_edge(*args):
    global PREVIEW_NODE, PREVIEW_NODE_ID, PREVIEW_TARGETS
    targets = list(PREVIEW_TARGETS)
    cmds.undoInfo(openChunk=True)
    try:
        if not targets:
            log_append("!! 请先选择目标组件，再拖拽滑块预览")
            return
        trans = cmds.ls(targets, objectsOnly=True)
        
        if not is_preview_node_valid():
            val = cmds.intSliderGrp(WIDGETS['hard_angle'], q=True, value=True)
            edges = cmds.filterExpand(cmds.polyListComponentConversion(targets, toEdge=True), selectionMask=32)
            if edges:
                cmds.polyNormalPerVertex(cmds.polyListComponentConversion(targets, toVertex=True), unFreezeNormal=True)
                cmds.polySoftEdge(edges, angle=val, constructionHistory=True)

        if trans:
            for t in trans:
                has_skin = False
                for node in (cmds.listHistory(t, pruneDagObjects=True) or []):
                    if cmds.nodeType(node) == 'skinCluster':
                        has_skin = True
                        break
                if has_skin: cmds.bakePartialHistory(t, prePostDeformers=True)
                else: cmds.delete(t, constructionHistory=True)
                    
        log_append("  [后期修复] 法线已成功固化 (保留蒙皮)！")
        PREVIEW_NODE = PREVIEW_NODE_ID = None
        PREVIEW_TARGETS = []

    except Exception as e:
        log_append("  !! 修复确认失败: {}".format(e))
    finally:
        cmds.undoInfo(closeChunk=True)
        set_viewport_highlight(True)
        if targets:
            try: cmds.select(targets, replace=True)
            except Exception: pass


# ════════════════════════════════════════════════════════════════
#  独立的三组拓扑选择逻辑 (纯净 OM2 C++ 内存极速计算 + 自适应感知)
# ════════════════════════════════════════════════════════════════

# ════════════════════════════════════════════════════════════════
#  智能判断：检测模型是否已沿 UV 纹理边界拆分
# ════════════════════════════════════════════════════════════════

def is_mesh_split_at_uv_borders(mesh_long_name):
    """
    检测模型是否已沿 UV 纹理边界拆分。
    原理：拆分后 UV 缝处顶点被 disconnect，产生空间位置完全重合的顶点。
    用 OM2 批量拿所有顶点坐标，对比实际顶点数 vs 去重后唯一位置数。
    """
    try:
        sel = om.MSelectionList()
        sel.add(mesh_long_name)
        dag = sel.getDagPath(0)
        mesh_fn = om.MFnMesh(dag)
        points = mesh_fn.getPoints(om.MSpace.kWorld)

        actual_count = len(points)
        unique_positions = set()
        for p in points:
            unique_positions.add((round(p.x, 5), round(p.y, 5), round(p.z, 5)))

        return actual_count > len(unique_positions)
    except Exception:
        return False


# ════════════════════════════════════════════════════════════════
#  独立的三组拓扑选择逻辑 (纯净 OM2 C++ 内存极速计算 + 自适应感知)
# ════════════════════════════════════════════════════════════════

def get_filtered_hard_components_om2(mesh_long_name, target_edges_list, is_split, return_type="edge"):
    """
    纯 OM2 内存极速计算。
    is_split=True 时，通过检测空间重合顶点来排除纹理边界上的硬边/硬边点。
    """
    final_components = []
    if not target_edges_list:
        return final_components

    try:
        sel = om.MSelectionList()
        sel.add(mesh_long_name)
        dag = sel.getDagPath(0)

        # 构建 O(1) 查询表
        target_indices = set()
        for e in target_edges_list:
            try:
                target_indices.add(int(e.split('.e[')[-1].split(']')[0]))
            except:
                pass

        # ========== 如果已拆分，先收集所有重合顶点索引 ==========
        border_vert_indices = set()
        if is_split:
            mesh_fn = om.MFnMesh(dag)
            points = mesh_fn.getPoints(om.MSpace.kWorld)

            # 按坐标分组，记录每个位置对应的顶点索引
            pos_to_verts = {}
            for i, p in enumerate(points):
                key = (round(p.x, 5), round(p.y, 5), round(p.z, 5))
                pos_to_verts.setdefault(key, []).append(i)

            # 同一位置有多个顶点 = 被 disconnect 过的纹理边界点
            for verts in pos_to_verts.values():
                if len(verts) > 1:
                    for v in verts:
                        border_vert_indices.add(v)

        # ========== 第一遍：收集硬边 ==========
        edge_iter = om.MItMeshEdge(dag)
        hard_edge_indices = set()

        while not edge_iter.isDone():
            idx = edge_iter.index()
            if idx in target_indices:
                if not edge_iter.isSmooth:
                    hard_edge_indices.add(idx)
            edge_iter.next()

        # ========== 第二遍：组装最终输出 ==========
        if return_type == "edge":
            edge_iter.reset()
            while not edge_iter.isDone():
                idx = edge_iter.index()
                if idx in hard_edge_indices:
                    if is_split:
                        v0 = edge_iter.vertexId(0)
                        v1 = edge_iter.vertexId(1)
                        # 排除：任一端点是重合顶点（纹理边界点）
                        if v0 not in border_vert_indices and v1 not in border_vert_indices:
                            final_components.append(f"{mesh_long_name}.e[{idx}]")
                    else:
                        final_components.append(f"{mesh_long_name}.e[{idx}]")
                edge_iter.next()

        elif return_type == "vertex":
            hard_vert_indices = set()
            edge_iter.reset()
            while not edge_iter.isDone():
                if edge_iter.index() in hard_edge_indices:
                    hard_vert_indices.add(edge_iter.vertexId(0))
                    hard_vert_indices.add(edge_iter.vertexId(1))
                edge_iter.next()

            for v_idx in hard_vert_indices:
                if is_split and v_idx in border_vert_indices:
                    continue
                final_components.append(f"{mesh_long_name}.vtx[{v_idx}]")

    except Exception as e:
        pass

    return final_components


# ════════════════════════════════════════════════════════════════
#  按钮1: 按纹理边界断开模型
# ════════════════════════════════════════════════════════════════

def on_split_texture_borders(*args):
    """按钮1: 按纹理边界断开模型"""
    current_sel = cmds.ls(selection=True, long=True)
    if not current_sel:
        log_append("!! 请先选择模型或组件(面/边)！")
        return

    echo_all = cmds.scriptEditorInfo(q=True, echoAllCommands=True)
    suppress = cmds.scriptEditorInfo(q=True, suppressResults=True)
    cmds.scriptEditorInfo(echoAllCommands=False, suppressResults=True)

    cmds.refresh(suspend=True)
    cmds.undoInfo(openChunk=True, chunkName="SplitTextureBorders")
    try:
        target_edges_raw = cmds.polyListComponentConversion(current_sel, toEdge=True)
        target_edges = cmds.ls(target_edges_raw, flatten=True, long=True) or []

        if not target_edges:
            return

        edges_by_mesh = {}
        for edge in target_edges:
            mesh = edge.split('.e[')[0]
            edges_by_mesh.setdefault(mesh, []).append(edge)

        split_count = 0
        cmds.polySelectConstraint(disable=True)

        for mesh, edges in edges_by_mesh.items():
            cmds.select(edges, replace=True)
            cmds.polySelectConstraint(mode=2, type=0x8000, border=True, uvConstraint=True)
            tb_edges = cmds.ls(selection=True, long=True) or []
            cmds.polySelectConstraint(disable=True)

            if tb_edges:
                cmds.polySplitEdge(tb_edges)
                split_count += len(tb_edges)

        if split_count > 0:
            log_append(f"  [拓扑断开] 已在选区内按纹理边界断开了 {split_count} 条边。")
        else:
            log_append(f"  [拓扑断开] 选区内未找到需要断开的纹理边界。")

        cmds.select(current_sel, replace=True)

    except Exception as e:
        log_append(f"!! 执行出错: {e}")
    finally:
        cmds.polySelectConstraint(disable=True)
        cmds.undoInfo(closeChunk=True)
        cmds.refresh(suspend=False)
        cmds.refresh(force=True)
        cmds.scriptEditorInfo(echoAllCommands=echo_all, suppressResults=suppress)


# ════════════════════════════════════════════════════════════════
#  按钮2: 选择硬边线
# ════════════════════════════════════════════════════════════════

def on_select_hard_edges_clean(*args):
    """按钮2: 选择硬边线 (OM2 极速版 + 重合顶点判定 + 安全分流)"""
    current_sel = cmds.ls(selection=True, long=True)
    if not current_sel:
        log_append("!! 请先选择模型或组件(面/边)！")
        return

    echo_all = cmds.scriptEditorInfo(q=True, echoAllCommands=True)
    suppress = cmds.scriptEditorInfo(q=True, suppressResults=True)
    cmds.scriptEditorInfo(echoAllCommands=False, suppressResults=True)

    cmds.undoInfo(openChunk=True, chunkName="SelectTargetEdges")
    cmds.refresh(suspend=True)
    try:
        target_edges_raw = cmds.polyListComponentConversion(current_sel, toEdge=True)
        target_edges = cmds.ls(target_edges_raw, flatten=True, long=True) or []

        if not target_edges:
            cmds.select(clear=True)
            return

        edges_by_mesh = {}
        for edge in target_edges:
            mesh = edge.split('.e[')[0]
            edges_by_mesh.setdefault(mesh, []).append(edge)

        all_final_edges = []

        for mesh, edges in edges_by_mesh.items():
            is_split = is_mesh_split_at_uv_borders(mesh)

            final_edges = get_filtered_hard_components_om2(mesh, edges, is_split, "edge")
            all_final_edges.extend(final_edges)

        if all_final_edges:
            cmds.select(all_final_edges, replace=True)
            cmds.selectMode(component=True)
            cmds.selectType(edge=True)
            log_append(f"  [选择确认] 闪电计算！已选中选区内 {len(all_final_edges)} 条硬边线。")
        else:
            cmds.select(clear=True)
            log_append("  [选择确认] 选区内未找到符合条件的边。")

    except Exception as e:
        log_append(f"!! 执行出错: {e}")
    finally:
        cmds.undoInfo(closeChunk=True)
        cmds.refresh(suspend=False)
        cmds.refresh(force=True)
        cmds.scriptEditorInfo(echoAllCommands=echo_all, suppressResults=suppress)


# ════════════════════════════════════════════════════════════════
#  按钮3: 选择硬边点
# ════════════════════════════════════════════════════════════════

def on_select_hard_verts_clean(*args):
    """按钮3: 选择硬边点 (OM2 极速版 + 重合顶点判定 + 物体/组件模式安全分流)"""
    current_sel = cmds.ls(selection=True, long=True)
    if not current_sel:
        log_append("!! 请先选择模型或组件(面/边)！")
        return

    echo_all = cmds.scriptEditorInfo(q=True, echoAllCommands=True)
    suppress = cmds.scriptEditorInfo(q=True, suppressResults=True)
    cmds.scriptEditorInfo(echoAllCommands=False, suppressResults=True)

    cmds.undoInfo(openChunk=True, chunkName="SelectTargetVerts")
    cmds.refresh(suspend=True)
    try:
        target_edges_raw = cmds.polyListComponentConversion(current_sel, toEdge=True)
        target_edges = cmds.ls(target_edges_raw, flatten=True, long=True) or []

        if not target_edges:
            cmds.select(clear=True)
            return

        original_verts_raw = cmds.polyListComponentConversion(current_sel, toVertex=True)
        original_verts = set(cmds.ls(original_verts_raw, flatten=True, long=True) or [])

        edges_by_mesh = {}
        for edge in target_edges:
            mesh = edge.split('.e[')[0]
            edges_by_mesh.setdefault(mesh, []).append(edge)

        all_final_verts = []

        for mesh, edges in edges_by_mesh.items():
            is_split = is_mesh_split_at_uv_borders(mesh)

            final_verts = get_filtered_hard_components_om2(mesh, edges, is_split, "vertex")
            all_final_verts.extend(final_verts)

        # ========== 汇总结果时，区分物体模式和组件模式 ==========
        is_component_sel = any(
            cmds.filterExpand(current_sel, selectionMask=m)
            for m in (31, 32, 34)
        )

        if is_component_sel and original_verts:
            final_output = [v for v in all_final_verts if v in original_verts]
        else:
            final_output = list(all_final_verts)

        if final_output:
            cmds.select(final_output, replace=True)
            cmds.selectMode(component=True)
            cmds.selectType(vertex=True)
            log_append(f"  [选择确认] 闪电计算！已选中 {len(final_output)} 个硬边点。")
        else:
            cmds.select(clear=True)
            log_append("  [选择确认] 选区内未找到符合条件的顶点。")

    except Exception as e:
        log_append(f"!! 执行出错: {e}")
    finally:
        cmds.undoInfo(closeChunk=True)
        cmds.refresh(suspend=False)
        cmds.refresh(force=True)
        cmds.scriptEditorInfo(echoAllCommands=echo_all, suppressResults=suppress)


# ════════════════════════════════════════════════════════════════
#  LOD 组智能重命名
# ════════════════════════════════════════════════════════════════

REMOVABLE_PATTERNS = [
    r"^LOD\d*$", r"^HD\d*$", r"^SD\d*$", r"^High\d*$", r"^Low\d*$",
    r"^Med\d*$", r"^Group\d*$", r"^Grp\d*$", r"^Assets\d*$",
    r"^mat\d*$", r"^v\d*$", r"^uv\d*$",
]

MIN_BASE_LENGTH = 4
REAL_NAME_THRESHOLD = 0.5

def short_name(node): return node.split("|")[-1].split(":")[-1]

def get_triangle_count(obj):
    shapes = cmds.listRelatives(obj, shapes=True, fullPath=True) or []
    if shapes and cmds.nodeType(shapes[0]) == "mesh": return cmds.polyEvaluate(shapes[0], triangle=True)
    return 0

def is_removable_part(part):
    for pat in REMOVABLE_PATTERNS:
        if re.match(pat, part, re.IGNORECASE): return True
    return False

def derive_base_name(children_names):
    if not children_names: return "Asset"
    split_names = [name.split("_") for name in children_names]
    max_len = max(len(s) for s in split_names)
    core_parts = []
    for idx in range(max_len):
        segments_at_idx = [s[idx] for s in split_names if idx < len(s)]
        if not segments_at_idx: continue
        freq = {}
        valid_count = 0
        for seg in segments_at_idx:
            if not is_removable_part(seg):
                freq[seg] = freq.get(seg, 0) + 1
                valid_count += 1
        if valid_count / len(segments_at_idx) >= REAL_NAME_THRESHOLD:
            chosen = max(freq.items(), key=lambda x: x[1])[0]
            core_parts.append(chosen)
        else: break
    base_name = "_".join(core_parts) if core_parts else "Asset"
    if len(base_name) < MIN_BASE_LENGTH: base_name = "Asset"
    return base_name

def process_single_group(group_node, log_fn):
    children = cmds.listRelatives(group_node, children=True, fullPath=True) or []
    mesh_children = [c for c in children if get_triangle_count(c) > 0]
    if not mesh_children:
        log_fn("  跳过组 {} : 内无 Mesh".format(short_name(group_node)))
        return

    child_short = [short_name(c) for c in mesh_children]
    base_name = derive_base_name(child_short)

    log_fn("\n" + "─" * 26)
    log_fn("  处理组: {}".format(short_name(group_node)))
    log_fn("  核心命名判定为: [{}]".format(base_name))

    data = [{"obj": c, "tri": get_triangle_count(c), "tmp": "", "orig_name": short_name(c)} for c in mesh_children]
    data.sort(key=lambda x: x["tri"], reverse=True)

    for i, d in enumerate(data):
        d["tmp"] = cmds.rename(d["obj"], "__TMP_{}_{}".format(i, base_name))

    if short_name(group_node) != base_name:
        try: cmds.rename(group_node, base_name)
        except Exception as e: log_fn("  组重命名警告: {}".format(e))

    for i, d in enumerate(data):
        expected_name = "{}_LOD{}".format(base_name, i)
        final_name = d["orig_name"] if d["orig_name"] == expected_name else expected_name
        cmds.rename(d["tmp"], final_name)
        log_fn("    LOD{} [Tri:{:,}] -> {}".format(i, d["tri"], final_name))

def on_run_lod_rename(*args):
    cmds.undoInfo(openChunk=True)
    try:
        selection = cmds.ls(selection=True, long=True)
        if not selection:
            log_append("!! 请选择一个或多个 Group")
            return
        log_append("\n=== LOD 组智能重命名 ===")
        processed = 0
        for obj in selection:
            if cmds.nodeType(obj) == "transform":
                process_single_group(obj, log_append)
                processed += 1
        if processed == 0:
            log_append("!! 选中的对象中没有有效的 Transform 组")
        else:
            log_append("=== 完成 {} 个组的 LOD 重命名 ===".format(processed))
    except Exception as e:
        log_append("!! LOD 重命名出错: {}".format(e))
    finally:
        cmds.undoInfo(closeChunk=True)


# ════════════════════════════════════════════════════════════════
#  Shell 定位 (调试用)
# ════════════════════════════════════════════════════════════════

def _analyze_shell_density(mesh):
    sel_list = om.MSelectionList()
    sel_list.add(mesh)
    try: dag = sel_list.getDagPath(0)
    except TypeError:
        dag = om.MDagPath()
        sel_list.getDagPath(0, dag)
    mfn = om.MFnMesh(dag)

    uv_set = mfn.currentUVSetName()
    nbUvShells, uvShellIds = mfn.getUvShellsIds(uv_set)
    all_u, all_v = mfn.getUVs(uv_set)

    shell_uv_area = {}
    shell_3d_area = {}
    shell_faces = {}

    for i in range(mfn.numPolygons):
        try: uv_id = mfn.getPolygonUVid(i, 0, uv_set)
        except Exception: continue
        sid = uvShellIds[uv_id]
        if sid not in shell_faces:
            shell_faces[sid] = []
            shell_uv_area[sid] = 0.0
            shell_3d_area[sid] = 0.0
        shell_faces[sid].append(i)

        nv = mfn.polygonVertexCount(i)
        uvs_u = []
        uvs_v = []
        for fv in range(nv):
            try:
                uid = mfn.getPolygonUVid(i, fv, uv_set)
                uvs_u.append(all_u[uid])
                uvs_v.append(all_v[uid])
            except Exception: pass
        if len(uvs_u) >= 3:
            a = sum(uvs_u[j] * uvs_v[(j + 1) % len(uvs_u)] - uvs_u[(j + 1) % len(uvs_u)] * uvs_v[j] for j in range(len(uvs_u)))
            shell_uv_area[sid] += abs(a) * 0.5

        pts = mfn.getPolygonVertices(i)
        if len(pts) >= 3:
            positions = [mfn.getPoint(p, om.MSpace.kWorld) for p in pts]
            area3d = sum((om.MVector(positions[j] - positions[0]) ^ om.MVector(positions[j + 1] - positions[0])).length() * 0.5 for j in range(1, len(positions) - 1))
            shell_3d_area[sid] += area3d

    densities = {sid: (shell_uv_area[sid] / shell_3d_area[sid] if shell_3d_area[sid] > 1e-12 else 0.0) for sid in shell_faces}
    sorted_d = sorted(densities.values())
    median_density = sorted_d[len(sorted_d) // 2] if len(sorted_d) % 2 == 1 else (sorted_d[len(sorted_d) // 2 - 1] + sorted_d[len(sorted_d) // 2]) / 2.0 if sorted_d else 0.0

    shell_info = {sid: {'faces': shell_faces[sid], 'density': densities[sid], 'ratio': densities[sid] / median_density if median_density > 0 else 0.0} for sid in shell_faces}
    return shell_info, median_density

def on_select_all_low(*args):
    sel = cmds.ls(selection=True, long=True)
    mesh = next((obj for obj in sel if cmds.listRelatives(obj, shapes=True, type='mesh')), None)
    if not mesh:
        log_append("!! 请先选中一个网格")
        return

    uv_threshold = cmds.floatField(WIDGETS['uv_thresh'], q=True, value=True)
    try:
        shell_info, median_density = _analyze_shell_density(mesh)
        low_faces, low_count, log_lines = [], 0, []
        for sid in sorted(shell_info.keys()):
            info = shell_info[sid]
            is_low = median_density > 0 and info['density'] < median_density * uv_threshold
            if is_low:
                low_count += 1
                low_faces.extend("{}.f[{}]".format(mesh, f) for f in info['faces'])
                log_lines.append(" * [Shell {:>2}]  面数:{:<4}  比值:{:>6.1f}% 低密度".format(sid, len(info['faces']), info['ratio'] * 100))

        if low_faces:
            cmds.select(low_faces, replace=True)
            log_append("  [定位] 已选中 {} 个低密度 Shell (共 {} 面)".format(low_count, len(low_faces)))
            for line in log_lines: log_append(line)
        else: log_append("  [定位] 未发现低密度 Shell")
    except Exception as e: log_append("!! 低密度定位失败: {}".format(e))

def on_select_shell(*args):
    shell_id = cmds.intField(WIDGETS['shell_id'], q=True, value=True)
    sel = cmds.ls(selection=True, long=True)
    mesh = next((obj for obj in sel if cmds.listRelatives(obj, shapes=True, type='mesh')), None)

    if not mesh:
        log_append("!! 请先选中一个网格，再点击定位")
        return

    try:
        shell_info, median_density = _analyze_shell_density(mesh)
        if shell_id not in shell_info:
            log_append("!! Shell {} 不存在，该模型共 {} 个 Shell (0~{})".format(shell_id, len(shell_info), max(shell_info.keys()) if shell_info else 0))
            return

        info = shell_info[shell_id]
        face_list = ["{}.f[{}]".format(mesh, f) for f in info['faces']]
        is_low = median_density > 0 and info['density'] < median_density * cmds.floatField(WIDGETS['uv_thresh'], q=True, value=True)

        if face_list:
            cmds.select(face_list, replace=True)
            log_append("  [定位] Shell {} : 已选中 {} 个面".format(shell_id, len(face_list)))
            log_append(" {} [Shell {:>2}]  面数:{:<4}  比值:{:>6.1f}%{}".format("*" if is_low else " ", shell_id, len(info['faces']), info['ratio'] * 100, " 低密度" if is_low else ""))
        else: log_append("  [定位] Shell {} : 未找到面".format(shell_id))
    except Exception as e: log_append("!! Shell 定位失败: {}".format(e))


# ════════════════════════════════════════════════════════════════
#  UI 逻辑 & 构建
# ════════════════════════════════════════════════════════════════

def log_append(msg):
    if cmds.workspaceControl(WINDOW_ID + "_Workspace", exists=True) and 'log' in WIDGETS:
        cur = cmds.scrollField(WIDGETS['log'], q=True, text=True) or ""
        cmds.scrollField(WIDGETS['log'], e=True, text=cur + msg + "\n")
    else: print(msg)

def on_mode_change(*args):
    use_tri = cmds.radioButtonGrp(WIDGETS['reduce_mode'], q=True, select=True) == 2
    cmds.intSliderGrp(WIDGETS['pct'],       e=True, enable=not use_tri)
    cmds.intFieldGrp (WIDGETS['tri_count'], e=True, enable=use_tri)

def _on_sym_type_changed(val):
    is_plane = (val == "平面")
    is_none  = (val == "无")
    cmds.floatField(WIDGETS['sym_tol'], e=True, enable=not is_none)
    cmds.optionMenu(WIDGETS['sym_plane'], e=True, enable=is_plane)

def on_run(*args):
    global PREVIEW_NODE, PREVIEW_NODE_ID, PREVIEW_TARGETS
    PREVIEW_NODE = PREVIEW_NODE_ID = None
    PREVIEW_TARGETS = []
    set_viewport_highlight(True)

    cmds.undoInfo(openChunk=True)
    try:
        use_tri      = cmds.radioButtonGrp(WIDGETS['reduce_mode'], q=True, select=True) == 2
        keep_pct     = cmds.intSliderGrp  (WIDGETS['pct'],         q=True, value=True)
        target_tris  = cmds.intFieldGrp   (WIDGETS['tri_count'],   q=True, value1=True)
        protect_skin = cmds.checkBox      (WIDGETS['cb_skin'],     q=True, value=True)
        do_del_int   = cmds.checkBox      (WIDGETS['cb_internal'], q=True, value=True)
        uv_thresh    = cmds.floatField    (WIDGETS['uv_thresh'],   q=True, value=True)
        triangulate  = cmds.checkBox      (WIDGETS['cb_tri'],      q=True, value=True)
        src_mode     = cmds.radioButtonGrp(WIDGETS['mode'],        q=True, select=True)
        sym_type     = cmds.optionMenu    (WIDGETS['sym_type'],    q=True, value=True)
        sym_tol      = cmds.floatField    (WIDGETS['sym_tol'],     q=True, value=True)
        sym_plane    = cmds.optionMenu    (WIDGETS['sym_plane'],   q=True, value=True)

        kwargs_reduce = {
            "version": 2, "sharpness": 0.5, "preserveTopology": 1, "triangulate": triangulate,
            "keepQuadsWeight": 0.0 if triangulate else 0.5,
            "keepBorder": cmds.checkBox(WIDGETS['cb_w_border'], q=True, value=True), "keepBorderWeight": cmds.floatSliderGrp(WIDGETS['w_border'], q=True, value=True),
            "keepMapBorder": cmds.checkBox(WIDGETS['cb_w_uv'], q=True, value=True), "keepMapBorderWeight": cmds.floatSliderGrp(WIDGETS['w_uv'], q=True, value=True),
            "keepColorBorder": cmds.checkBox(WIDGETS['cb_w_color'], q=True, value=True), "keepColorBorderWeight": cmds.floatSliderGrp(WIDGETS['w_color'], q=True, value=True),
            "keepFaceGroupBorder": cmds.checkBox(WIDGETS['cb_w_material'], q=True, value=True), "keepFaceGroupBorderWeight": cmds.floatSliderGrp(WIDGETS['w_material'], q=True, value=True),
            "keepHardEdge": cmds.checkBox(WIDGETS['cb_w_hard'], q=True, value=True), "keepHardEdgeWeight": cmds.floatSliderGrp(WIDGETS['w_hard'], q=True, value=True),
            "keepCreaseEdge": cmds.checkBox(WIDGETS['cb_w_crease'], q=True, value=True), "keepCreaseEdgeWeight": cmds.floatSliderGrp(WIDGETS['w_crease'], q=True, value=True),
        }

        if sym_type == "自动": kwargs_reduce.update({"useVirtualSymmetry": 1, "symmetryTolerance": sym_tol})
        elif sym_type == "平面":
            px, py, pz = {"XZ": (0, 1, 0), "XY": (0, 0, 1), "YZ": (1, 0, 0)}.get(sym_plane, (0, 1, 0))
            kwargs_reduce.update({"useVirtualSymmetry": 2, "symmetryPlaneX": px, "symmetryPlaneY": py, "symmetryPlaneZ": pz, "symmetryPlaneW": 0, "symmetryTolerance": sym_tol})

        cmds.scrollField(WIDGETS['log'], e=True, text="")
        log_append("=== Native Mesh Reducer ===")

        meshes = []
        if src_mode == 1:
            meshes = [obj for obj in (cmds.ls(selection=True, long=True) or []) if cmds.listRelatives(obj, shapes=True, type='mesh')]
        else:
            seen = set()
            for sc in (cmds.ls(type='skinCluster') or []):
                for geo in (cmds.skinCluster(sc, q=True, geometry=True) or []):
                    p = (cmds.listRelatives(geo, parent=True) or [None])[0]
                    if p and p not in seen:
                        meshes.append(p)
                        seen.add(p)

        if not meshes:
            log_append("!! 请选择网格")
            return

        ok = sum(1 for m in meshes if do_reduce(m, use_tri, keep_pct, target_tris, kwargs_reduce, protect_skin, triangulate, do_del_int, uv_thresh, log_append))

        if meshes:
            valid_lods = [m for m in [m.split('|')[-1].split(':')[-1] + "_LOD" for m in meshes] if cmds.objExists(m)]
            if valid_lods: cmds.select(valid_lods, replace=True)

        log_append("=== 完成 {}/{} | LOD 已生成 ===".format(ok, len(meshes)))

    finally: cmds.undoInfo(closeChunk=True)

def on_run_lod_rename_with_ui(*args):
    global MIN_BASE_LENGTH, REAL_NAME_THRESHOLD
    MIN_BASE_LENGTH = cmds.intField(WIDGETS['lod_min_len'], q=True, value=True)
    REAL_NAME_THRESHOLD = cmds.intField(WIDGETS['lod_threshold'], q=True, value=True) / 100.0
    on_run_lod_rename()

def show_ui():
    DOCK_ID = WINDOW_ID + "_Workspace"
    if cmds.workspaceControl(DOCK_ID, exists=True):
        cmds.workspaceControl(DOCK_ID, edit=True, restore=True)
        cmds.workspaceControl(DOCK_ID, edit=True, visible=True)
        cmds.evalDeferred('import maya.cmds as cmds; cmds.workspaceControl("{}", edit=True, collapse=False); cmds.setFocus("{}")'.format(DOCK_ID, DOCK_ID))
        log_append("  [提示] 减面工具面板已唤醒。")
    else: build_ui()

def build_ui():
    global WIDGETS
    DOCK_ID = WINDOW_ID + "_Workspace"

    if cmds.window(WINDOW_ID, exists=True): cmds.deleteUI(WINDOW_ID)
    if cmds.dockControl(WINDOW_ID + "_Dock", exists=True): cmds.deleteUI(WINDOW_ID + "_Dock")
    if cmds.workspaceControl(DOCK_ID, exists=True):
        cmds.closeWorkspaceControl(DOCK_ID)
        cmds.deleteUI(DOCK_ID)

    cmds.workspaceControl(DOCK_ID, retain=False, floating=False, label="智能减面工具", initialWidth=440, minimumWidth=440, tabToControl=['AttributeEditor', -1])
    cmds.scrollLayout(horizontalScrollBarThickness=0, verticalScrollBarThickness=16)
    
    main_col = cmds.columnLayout(adjustableColumn=True, rowSpacing=4, columnOffset=('both', 8))
    cmds.scriptJob(uiDeleted=[main_col, on_ui_close])

    cmds.separator(height=6, style='none')
    cmds.text(label="Native Mesh Reducer", font="boldLabelFont", height=24)
    cmds.separator(height=4, style='in')

    # ── 1. 减面执行 ──
    cmds.frameLayout(label="1. 减面执行 (基于 Maya 底层)", collapsable=True, collapse=False, marginWidth=8, marginHeight=6)
    WIDGETS['mode'] = cmds.radioButtonGrp(numberOfRadioButtons=2, labelArray2=["选中的网格", "所有蒙皮网格"], select=1, columnWidth2=(200, 200))
    WIDGETS['reduce_mode'] = cmds.radioButtonGrp(numberOfRadioButtons=2, labelArray2=["按百分比保留", "精确三角面数"], select=1, columnWidth2=(200, 200), changeCommand=on_mode_change)

    cmds.rowLayout(numberOfColumns=2, columnWidth2=(200, 200), columnAlign2=('left', 'left'))
    WIDGETS['pct'] = cmds.intSliderGrp(label="保留%", field=True, minValue=5, maxValue=95, value=50, columnWidth3=(44, 38, 114))
    WIDGETS['tri_count'] = cmds.intFieldGrp(numberOfFields=1, label="目标三角数", value1=2000, columnWidth2=(80, 90), enable=False)
    cmds.setParent('..')

    cmds.rowLayout(numberOfColumns=6, columnWidth6=(65, 80, 55, 80, 60, 80), columnAlign6=('left','left','left','left','left','left'))
    cmds.text(label="  对称:")
    WIDGETS['sym_type'] = cmds.optionMenu(changeCommand=_on_sym_type_changed)
    cmds.menuItem(label="无"); cmds.menuItem(label="自动"); cmds.menuItem(label="平面")
    cmds.text(label="  容差:")
    WIDGETS['sym_tol'] = cmds.floatField(value=0.01, minValue=0.0001, maxValue=10.0, precision=4, width=70, enable=False)
    cmds.text(label="  平面:")
    WIDGETS['sym_plane'] = cmds.optionMenu(enable=False)
    cmds.menuItem(label="XZ"); cmds.menuItem(label="XY"); cmds.menuItem(label="YZ")
    cmds.setParent('..')

    cmds.rowLayout(numberOfColumns=3, columnWidth3=(90, 180, 110), columnAlign3=('left', 'left', 'left'))
    WIDGETS['cb_skin'] = cmds.checkBox(label="保留蒙皮", value=True)
    cmds.rowLayout(numberOfColumns=3, columnWidth3=(75, 40, 50))
    WIDGETS['cb_internal'] = cmds.checkBox(label="删内部面", value=False)
    cmds.text(label=" 阈值:")
    WIDGETS['uv_thresh'] = cmds.floatField(value=0.5, minValue=0.0, maxValue=1.0, precision=2)
    cmds.setParent('..')
    WIDGETS['cb_tri'] = cmds.checkBox(label="强制三角输出", value=True)
    cmds.setParent('..')

    cmds.button(label="执行原生减面", height=34, backgroundColor=(0.25, 0.45, 0.25), command=on_run)

    cmds.rowLayout(numberOfColumns=4, columnWidth4=(90, 45, 120, 120), columnAlign4=('left', 'left', 'left', 'left'))
    cmds.text(label="  Shell定位:")
    WIDGETS['shell_id'] = cmds.intField(value=0, minValue=0, width=40)
    cmds.button(label="选中该Shell", width=100, height=22, backgroundColor=(0.3, 0.35, 0.5), command=on_select_shell)
    cmds.button(label="选中所有低密度", width=100, height=22, backgroundColor=(0.5, 0.3, 0.3), command=on_select_all_low)
    cmds.setParent('..')
    cmds.setParent('..')

    # ── 2. 高级特征保护 ──
    cmds.frameLayout(label="2. 高级特征保护 (打勾启用, 0.0忽略 - 1.0锁定)", collapsable=True, collapse=False, marginWidth=8, marginHeight=6)
    def _w_row(k, l, d):
        cmds.rowLayout(numberOfColumns=2, columnWidth2=(135, 275), columnAlign2=('left', 'left'))
        WIDGETS['cb_w_'+k] = cmds.checkBox(label=l, value=True, changeCommand=lambda state, key=k: cmds.floatSliderGrp(WIDGETS['w_'+key], edit=True, enable=state))
        WIDGETS['w_'+k] = cmds.floatSliderGrp(field=True, minValue=0.0, maxValue=1.0, value=d, precision=2, columnWidth3=(1, 45, 220), enable=True)
        cmds.setParent('..')
    _w_row('border', "外边界 (Border)", 0.5)
    _w_row('uv', "UV 边界 (Map)", 0.5)
    _w_row('color', "颜色边界 (Color)", 0.5)
    _w_row('material', "材质边界 (Mat)", 0.5)
    _w_row('hard', "硬边 (Hard)", 0.5)
    _w_row('crease', "折痕边 (Crease)", 0.5)
    cmds.setParent('..')

    # ── 3. 后期修复 ──
    cmds.separator(height=6, style='in')
    cmds.frameLayout(label="3. 后期修复 (实时无损预览)", collapsable=True, collapse=False, marginWidth=8, marginHeight=6)
    cmds.text(label="选择目标组件后拖拽滑块预览，点击确认固化效果", align='left', font='smallPlainLabelFont')
    
    cmds.rowLayout(numberOfColumns=3, columnWidth3=(250, 60, 60), adjustableColumn=1, columnAlign3=('left', 'right', 'right'))
    WIDGETS['hard_angle'] = cmds.intSliderGrp(label="二面角", field=True, minValue=0, maxValue=180, value=35, columnWidth3=(50, 40, 150), dragCommand=preview_soft_edge, changeCommand=preview_soft_edge)
    cmds.button(label="还原", width=56, height=26, backgroundColor=(0.5, 0.3, 0.3), command=cancel_soft_edge)
    cmds.button(label="确认", width=56, height=26, backgroundColor=(0.25, 0.45, 0.25), command=apply_soft_edge)
    cmds.setParent('..')
    
    cmds.separator(height=6, style='none')
    cmds.rowLayout(numberOfColumns=3, columnWidth3=(135, 135, 135), columnAlign3=('center', 'center', 'center'))
    cmds.button(label="拆出纹理边界", width=125, height=26, backgroundColor=(0.5, 0.3, 0.3), command=on_split_texture_borders)
    cmds.button(label="选择硬边线", width=125, height=26, backgroundColor=(0.25, 0.45, 0.25), command=on_select_hard_edges_clean)
    cmds.button(label="选择硬边点", width=125, height=26, backgroundColor=(0.3, 0.35, 0.5), command=on_select_hard_verts_clean)
    cmds.setParent('..')
    cmds.setParent('..')

    # ── 4. LOD 组智能重命名 ──
    cmds.separator(height=6, style='in')
    cmds.frameLayout(label="4. LOD 组智能重命名 (选组 -> 按面数降序)", collapsable=True, collapse=False, marginWidth=8, marginHeight=6)

    cmds.rowLayout(numberOfColumns=4, columnWidth4=(90, 60, 120, 60), columnAlign4=('left', 'left', 'left', 'left'))
    cmds.text(label="名称最短长度:")
    WIDGETS['lod_min_len'] = cmds.intField(value=4, minValue=1, maxValue=20, width=40)
    cmds.text(label="  真名判定阈值(%):")
    WIDGETS['lod_threshold'] = cmds.intField(value=50, minValue=10, maxValue=100, width=40)
    cmds.setParent('..')

    cmds.separator(height=4, style='none')
    cmds.button(label="执行 LOD 组重命名", height=30, backgroundColor=(0.3, 0.35, 0.5), command=on_run_lod_rename_with_ui)
    cmds.setParent('..')

    # ── 5. 日志 ──
    cmds.frameLayout(label="执行日志", collapsable=True, collapse=False, marginWidth=6, marginHeight=6)
    WIDGETS['log'] = cmds.scrollField(editable=False, wordWrap=True, height=350, font='fixedWidthFont', backgroundColor=(0.13, 0.13, 0.13))
    cmds.setParent('..')
    
    cmds.setParent('..')
    cmds.setParent('..')

    cmds.workspaceControl(DOCK_ID, edit=True, visible=True)
    cmds.workspaceControl(DOCK_ID, edit=True, restore=True)
    cmds.evalDeferred('import maya.cmds as cmds; cmds.workspaceControl("{}", edit=True, collapse=False)'.format(DOCK_ID))

if __name__ == "__main__":
    show_ui()