from arcpy import AddError, env, CheckExtension, Describe, GetArgumentCount, GetParameter, ListFields
from arcpy.analysis import MultipleRingBuffer
from arcpy.conversion import PolygonToRaster
from arcpy.da import UpdateCursor
from arcpy.management import AddField, CopyFeatures, Delete, GetRasterProperties
from arcpy.mapping import AddLayer, Layer, ListDataFrames, MapDocument
from arcpy import sa
from os.path import join as osjoin
import math # DO NOT REMOVE - is not explicitly used but the user may want it later when inputting z_func parameter


def make_z_dict(distance_list, stair_type, z_func):

    """
        Helper function for main function raster_stamp().  Compiles a list of values at which the distance function is
        evaluated, which can then later be used to populate the z field in the resulting feature class.

        @param distance_list The list of values at which to evaluate the height function.
        @type distance_list list
        @param stair_type Determines whether the z_value assigned to the entire terrace should originate from the
                          innermost edge of the terrace ("INSIDE"), the outermost edge ("OUTSIDE"), or the centre
                          ("CENTRE").
        @type stair_type list
        @param z_func The single variable distance function which will evaluate discrete distances in distance_list
        @type z_func string

        @return dict
    """

    z_values_dict = {}
    previous_distance = 0

    for distance in distance_list:

        # find di, which can be at the centre of the buffer distances, the inside edge or the outside edge.
        if stair_type == 'CENTRE':
            di = float(previous_distance) + ((float(distance) - float(previous_distance)) / 2)
        elif stair_type == 'INSIDE':
            di = float(previous_distance)
        elif stair_type == 'OUTSIDE':
            di = float(distance)
        else:
            raise ValueError('Invalid stair_type parameter.  Valid values are "CENTRE", "INSIDE" and "OUTSIDE".')

        # evaluate z value at di
        z_values_dict[float(distance)] = eval(z_func.replace('d', str(di)))

        previous_distance = distance

    return z_values_dict


def raster_stamp(in_fc, in_raster, out_raster, operation, distance_list, z_func, stair_type, buffer_unit,
                 dissolve_option, outside_polygons_only, cell_assignment):

    """
        Creates a series of buffers from an input feature class, and creates a field on the output feature class
        which contains z values derived from an input function f(z).

        @param in_fc Features from which to generate the buffers used to make the stamp raster.
        @type in_fc Feature Layer
        @param in_raster Surface raster onto which the stamp raster will be stamped.
        @type in_raster Raster Layer
        @param operation How to apply the stamp to the surface raster.
        @type operation string (accepted values: 'ADD', 'SUBTRACT', 'MULTIPLY', 'DIVIDE')
        @param distance_list The list of values at which to evaluate the height function.
        @type distance_list list, double
        @param z_func The single variable height function, which will evaluate d values for distances in distance_list
        @type z_func string, (must be a valid Python 2.7.13 statement, use 'd' as the distance variable
                             (ex. 'd**2 + d + 1'))
        @param stair_type Determines whether the z_value assigned to each successive buffer should originate from the
                          innermost edge of the buffers the outermost edge or the centre.
        @type string (accepted values: 'CENTRE', 'INSIDE', 'OUTSIDE')
        @param buffer_unit Units used to calculate the buffer distances.  See documention for
                           arcpy.management.MultipleRingBuffer.
        @type buffer_unit string
        @param dissolve_option Determines if colliding buffers from other features will be dissolved.  See documention
                               for arcpy.management.MultipleRingBuffer.
        @type dissolve_option string
        @param outside_polygons_only Determines if buffers from polygon inputs will cover input features.  See
                                     documention for arcpy.management.MultipleRingBuffer.
        @type outside_polygons_only boolean
        @param cell_assignment Method by which the cells in the stamp raster are assigned.  See documentation for
                               arcpy.conversion.PolygonToRaster.
        @type cell_assignment string (accepted values: 'CELL_CENTER', 'MAXIMUM_AREA', 'MAXIMUM_COMBINED_AREA')

        @return Raster Dataset
    """

    buffer_fc = osjoin(env.scratchGDB, 'stamp_buffer')
    stamp_raster = osjoin(env.scratchGDB, 'stamp_raster')

    # When trying to run the tool via the GUI, and using layers in the table of contents (drag-and-drop) it kept
    # failing, referencing an error in the ESRI analysis.py code.  It only worked when using absolute file paths.  These
    # two try-except blocks neatly bypass this problem behind the scenes.
    try:
        # replace the parameter with its own data source.
        in_fc = in_fc.dataSource
    except:
        # if that didn't work, it's not a layer object, therefore it's already an absolute path, so just leave it be.
        pass
    try:
        in_raster = in_raster.dataSource
    except:
        pass

    try:
        # make the buffers, add a z field, and populate the field with the z values at the specified distances
        
        MultipleRingBuffer(in_fc, buffer_fc, sorted(distance_list), buffer_unit, 'distance', dissolve_option,
                           outside_polygons_only)
        AddField(buffer_fc, 'z_value', 'DOUBLE')
        z_dict = make_z_dict(sorted(distance_list), stair_type, z_func)

        with UpdateCursor(buffer_fc, ['distance', 'z_value']) as cursor:
            for row in cursor:
                row[1] = z_dict[row[0]]
                cursor.updateRow(row)
        del row
        del cursor

        # There is a file lock that just won't go away here, I thought it was the UpdateCursor but all traces of it
        # should be deleted at this point.  I think it has something to do with MultipleRingBuffer.  In any case,
        # copying the features to a new feature class fixes it.
        CopyFeatures(buffer_fc, buffer_fc+'nolock')

        # Get the properties of the input raster so we can make sure the stamp raster fits the surface raster, then
        # convert the buffers to the stamp raster.  Replace commas with periods because non-english systems will break
        # otherwise (other languages use commas instead of periods as decimal points).
        cell_size = float(GetRasterProperties(in_raster, 'CELLSIZEX').getOutput(0).replace(',', '.'))
        env.snapRaster = in_raster
        PolygonToRaster(buffer_fc+'nolock', 'z_value', stamp_raster, cell_assignment=cell_assignment,
                        cellsize=cell_size)

        # set up inputs for the Raster Calculator and set processing extent to make sure the entire surface raster is
        # carried over to the output and not just the extent of the stamp.
        surface = sa.Raster(str(in_raster))
        stamp = sa.Raster(str(stamp_raster))
        env.extent = surface.extent

        # stamp the stamp raster onto the surface raster, but make sure to keep all the cell values from the input
        # raster if they don't coincide with the stamp area.  then save the output raster.
        if operation == 'ADD':
            outSurface = sa.Con(sa.IsNull(stamp), surface, surface+stamp)
        elif operation == 'SUBTRACT':
            outSurface = sa.Con(sa.IsNull(stamp), surface, surface-stamp)
        elif operation == 'MULTIPLY':
            outSurface = sa.Con(sa.IsNull(stamp), surface, surface*stamp)
        elif operation == 'DIVIDE':
            outSurface = sa.Con(sa.IsNull(stamp), surface, surface/stamp)
        outSurface.save(str(out_raster))

        # add the output to the table of contents if the user is running it from the GUI.
        try:
            AddLayer(ListDataFrames(MapDocument('CURRENT'))[0], Layer(out_raster))
        except:
            pass

    # cleanup even if there is a failure so we don't have old junk floating around.
    finally:
        try:
            Delete(buffer_fc)
        except:
            pass
        try:
            Delete(buffer_fc + 'nolock')
        except:
            pass
        try:
            Delete(stamp_raster)
        except:
            pass

    # return a reference to the output (stamped) raster for easy modularity for use in larger scripts.
    return out_raster


if __name__ == '__main__':

    if CheckExtension('Spatial') != 'Available':
        raster_stamp(*[GetParameter(i) for i in range(GetArgumentCount())])
    else:
        AddError('\tThe Raster Stamp requires ArcGIS Spatial Analyst Extension to function.  Please activate the extension and try again.')
