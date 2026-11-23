// Endpoint sweeps deliberately bypass physics and expressions, just like the comparison pane.
export function measureInfluences(model, inventory, rawLabels, wardrobe) {
  const p = model.parameters, d = model.drawables
  const px = model.canvasinfo.PixelsPerUnit * 2048 / Math.max(model.canvasinfo.CanvasWidth,model.canvasinfo.CanvasHeight)
  const outfits = {}
  for (const option of wardrobe.options) {
    p.values.set(p.defaultValues)
    const knot = wardrobe.knots.indexOf(option.value)
    for (const [raw,values] of Object.entries(wardrobe.source)) p.values[p.ids.indexOf(raw)] = values[knot]
    model.update()
    const defaults = p.values.slice(), neutral = d.vertexPositions.map(coords => coords.slice())
    const alpha = d.opacities.slice(), order = d.renderOrders.slice()
    const controls = []
    for (const [j,id] of p.ids.entries()) {
      const effects = new Map()
      for (const endpoint of [p.minimumValues[j],p.maximumValues[j]]) {
        if (endpoint === defaults[j]) continue
        p.values.set(defaults)
        p.values[j] = endpoint
        model.update()
        for (const [i,part] of inventory.entries()) {
          if (!d.indexCounts[i] || Math.max(alpha[i],d.opacities[i]) <= .001) continue
          const points = d.vertexPositions[i]
          let distance = 0
          for (let k=0; k<points.length; k+=2) {
            distance = Math.max(distance, Math.hypot(points[k]-neutral[i][k],points[k+1]-neutral[i][k+1])*px)
          }
          const opacity = Math.abs(d.opacities[i]-alpha[i])
          const reorder = d.renderOrders[i] !== order[i]
          if (distance < .02 && opacity < .001 && !reorder) continue
          const prior = effects.get(part.id)
          effects.set(part.id,{id:part.id,group:part.group,
            maxTravelPx:Math.max(prior?.maxTravelPx ?? 0,Math.round(distance*100)/100),
            maxOpacityChange:Math.max(prior?.maxOpacityChange ?? 0,Math.round(opacity*1000)/1000),
            orderChanged:reorder || !!prior?.orderChanged})
        }
      }
      controls.push({raw:id,label:rawLabels.get(id),min:p.minimumValues[j],neutral:defaults[j],max:p.maximumValues[j],
        effects:[...effects.values()].sort((a,b) => b.maxTravelPx-a.maxTravelPx)})
    }
    outfits[option.label] = controls
  }
  return {canvasLongEdge:2048,outfits}
}
