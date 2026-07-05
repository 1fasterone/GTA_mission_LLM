using UnityEngine;
using UnityEngine.Rendering;
using UnityEngine.Rendering.RenderGraphModule;
using UnityEngine.Rendering.Universal;
using System.Collections.Generic;

#if UNITY_EDITOR
using UnityEditor.SceneManagement;
#endif


public class AtmosphereRenderPass : ScriptableRenderPass
{
    struct SortedEffect
    {
        public AtmosphereEffect effect;
        public float distanceToEffect;
    }


    private static Shader atmosphereShader;

    private static readonly int sourceId = Shader.PropertyToID("_Source");


    public AtmosphereRenderPass(Shader atmosphereShader)
    {
        AtmosphereRenderPass.atmosphereShader = atmosphereShader;

        // The pass samples and rewrites the camera color target, so URP must
        // render through an intermediate texture rather than the backbuffer.
        requiresIntermediateTexture = true;
    }


    private static readonly List<AtmosphereEffect> currentActiveEffects = new();


    public static void RegisterEffect(AtmosphereEffect effect)
    {
        if (!currentActiveEffects.Contains(effect))
        {
            currentActiveEffects.Add(effect);
        }
    }


    public static void RemoveEffect(AtmosphereEffect effect)
    {
        currentActiveEffects.Remove(effect);
    }


    private readonly List<SortedEffect> visibleEffects = new();

    private Plane[] cameraPlanes;


    private void CullAndSortEffects(Camera camera)
    {
        visibleEffects.Clear();

        // Perform culling of active effects
        cameraPlanes = GeometryUtility.CalculateFrustumPlanes(camera);

        Vector3 viewPos = camera.transform.position;

        for (int i = currentActiveEffects.Count - 1; i >= 0; i--)
        {
            if (currentActiveEffects[i] == null)
            {
                currentActiveEffects.RemoveAt(i);
                continue;
            }

            if (currentActiveEffects[i].IsVisible(cameraPlanes))
            {
                float dstToSurface = currentActiveEffects[i].DistToAtmosphere(viewPos);

                visibleEffects.Add(new SortedEffect {
                    effect = currentActiveEffects[i],
                    distanceToEffect = dstToSurface
                });
            }
        }

        // Sort effects from far to near
        for (int i = 0; i < visibleEffects.Count - 1; i++)
        {
            for (int j = i + 1; j > 0; j--)
            {
                if (visibleEffects[j - 1].distanceToEffect < visibleEffects[j].distanceToEffect)
                {
                    // Swap elements
                    (visibleEffects[j], visibleEffects[j - 1]) = (visibleEffects[j - 1], visibleEffects[j]);
                }
            }
        }
    }


    private class PassData
    {
        public readonly List<Material> materials = new();
        public TextureHandle color;
        public TextureHandle tempA;
        public TextureHandle tempB;
    }


    public override void RecordRenderGraph(RenderGraph renderGraph, ContextContainer frameData)
    {
        UniversalCameraData cameraData = frameData.Get<UniversalCameraData>();
        UniversalResourceData resourceData = frameData.Get<UniversalResourceData>();

        if (!cameraData.postProcessEnabled || cameraData.isPreviewCamera)
        {
            return;
        }

        CullAndSortEffects(cameraData.camera);

        if (visibleEffects.Count == 0)
        {
            return;
        }

#if UNITY_EDITOR
        bool prefabMode = PrefabStageUtility.GetCurrentPrefabStage() != null;
#else
        bool prefabMode = false;
#endif

        // The effect chain ping-pongs between two intermediate textures via a
        // full-screen material blit per effect, then writes the result back to
        // the camera color target. Legacy CommandBuffer.Blit is unavailable
        // under RenderGraph, so this runs as an unsafe pass using Blitter.
        using (var builder = renderGraph.AddUnsafePass<PassData>("Atmosphere Effects", out PassData passData))
        {
            passData.materials.Clear();

            foreach (SortedEffect visible in visibleEffects)
            {
#if UNITY_EDITOR
                if (prefabMode && PrefabStageUtility.GetPrefabStage(visible.effect.gameObject) == null)
                {
                    continue;
                }
#endif
                Material blitMat = visible.effect.GetMaterial(atmosphereShader);
                if (blitMat != null)
                {
                    passData.materials.Add(blitMat);
                }
            }

            if (passData.materials.Count == 0)
            {
                return;
            }

            RenderTextureDescriptor desc = cameraData.cameraTargetDescriptor;
            desc.depthBufferBits = 0;
            desc.msaaSamples = 1;

            passData.color = resourceData.activeColorTexture;
            passData.tempA = UniversalRenderer.CreateRenderGraphTexture(
                renderGraph, desc, "_AtmosphereBlitA", false, FilterMode.Bilinear);
            passData.tempB = UniversalRenderer.CreateRenderGraphTexture(
                renderGraph, desc, "_AtmosphereBlitB", false, FilterMode.Bilinear);

            builder.UseTexture(passData.color, AccessFlags.ReadWrite);
            builder.UseTexture(passData.tempA, AccessFlags.ReadWrite);
            builder.UseTexture(passData.tempB, AccessFlags.ReadWrite);
            builder.AllowPassCulling(false);

            builder.SetRenderFunc((PassData data, UnsafeGraphContext ctx) =>
            {
                CommandBuffer cmd = CommandBufferHelpers.GetNativeCommandBuffer(ctx.cmd);

                RTHandle current = data.color;
                bool nextIsA = true;

                for (int i = 0; i < data.materials.Count; i++)
                {
                    RTHandle dest = nextIsA ? (RTHandle)data.tempA : (RTHandle)data.tempB;
                    nextIsA = !nextIsA;

                    cmd.SetGlobalTexture(sourceId, current);
                    Blitter.BlitCameraTexture(cmd, current, dest, data.materials[i], 0);

                    current = dest;
                }

                // Blit the final chain output back into the camera color target.
                Blitter.BlitCameraTexture(cmd, current, data.color);
            });
        }
    }
}
