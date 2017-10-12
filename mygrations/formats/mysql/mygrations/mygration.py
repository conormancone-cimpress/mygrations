import copy

from mygrations.formats.mysql.definitions.database import database
from mygrations.formats.mysql.mygrations.operations.alter_table import alter_table
from mygrations.formats.mysql.mygrations.operations.add_constraint import add_constraint
from mygrations.formats.mysql.mygrations.operations.remove_table import remove_table

class mygration:
    """ Creates a migration plan to update a database to a given spec.

    If only one database is passed in, it treats it as a structure to migrate
    to starting from a blank slate, which primariliy means just figuring out
    what order to add tables in to account for foreign key constraints.

    If two tables are present, then it will treat the second as the current
    database constraint, and will figure out steps to execute in action
    to update that second database to match the structure of the first.

    The general steps are:

    1. Add/update columns that exist in both, temporarily skipping FK constraints that aren't yet fulfilled
    2. Remove any foreign keys that don't exist in the target database structure
    3. Add new tables in order necessitated by FK constraints.  Temporarily skip any FK constraints that can't be fulfilled yet
    4. Add in all FKs that were previously skipped
    5. Remove any columns that do not exist in the target database structure, noting an error if a FK is violated
    6. Remove any tables that do not exist in the target database structure, noting an error if a FK is violated
    """
    def __init__( self, db_to, db_from = None ):
        """ Create a migration plan

        :param db_to: The target database structure to migrate to
        :param db_from: The current database structure to migrate from
        :type db_to: mygrations.formats.mysql.definitions.database
        :type db_from: mygrations.formats.mysql.definitions.database
        """

        self.db_to = db_to
        self.db_from = db_from
        [ self._errors_1215, self._operations ] = self._process()

    @property
    def operations( self ):
        """ Public getter.  Returns list of operations to bring db_from to db_to

        If db_from doesn't exist then it will be a list of operations to
        create db_to.

        :returns: A list of table operations
        :rtype: [mygrations.formats.mysql.mygrations.operations.operation]
        """
        return self._operations

    @property
    def errors_1215( self ):
        """ Public getter.  Returns list of 1215 errors (as strings)

        :returns: A list of 1215 error messages
        :rtype: [string]
        """
        return self._errors_1215

    def __len__( self ):
        return len( self._operations )

    def __bool__( self ):
        return True if len( self._operations ) else False

    def __str__( self ):
        return "\n".join( [ str( x ) for x in self._operations ] )

    def __iter__( self ):
        return self._operations.__iter__()

    def _differences(self, a, b):
        """
        Calculates the difference between two OrderedDicts.

        https://codereview.stackexchange.com/a/176303/140581

        Duplication!!!! (formats.mysql.create_parser).  Sue me.

        :param a: OrderedDict
        :param b: OrderedDict
        :return: (added, removed, overlap)
        """

        return (
            [key for key in b if key not in a],
            [key for key in a if key not in b],
            [key for key in a if key in b]
        )

    def _process( self ):
        """ Figures out the operations (and proper order) need to get to self.db_to

        Excessively commented because there are a lot of details and this is a critical
        part of the process
        """

        # Our primary output is a list of operations, but there is more that we need
        # to make all of this happen.  We need a database to keep track of the
        # state of the database we are building after each operation is "applied"
        tracking_db = copy.deepcopy( self.db_from ) if self.db_from else database()

        # a little bit of extra processing will simplify our algorithm by a good chunk.
        # The situation is much more complicated when we have a database we are migrating
        # from, because columns might be added/removed/changed, and it might be (for instance)
        # that the removal of a column breaks a foreign key constraint.  The general
        # ambiguities introduced by changes happening in different times/ways makes it
        # much more difficult to figure out when foreign key constraints can properly
        # be added without triggering a 1215 error.  The simplest way to straighten this
        # all out is to cheat: "mygrate" the "to" database all by itself.  Without a "from"
        # the operations are more straight-forward, and we can figure out with less effort
        # whether or not all FK constraints can be fulfilled.  If they aren't all fulfilled,
        # then just quit now before we do anything.  If they are all fulfilled then we
        # know our final table will be fine, so if we can just split off any uncertain
        # foreign key constraints and apply them all at the end when our database is done
        # being updated.  Simple(ish)!
        if self.db_from:
            check = mygration( self.db_to )
            if check.errors_1215:
                return [ check.errors_1215, [] ]

        # First figure out the status of individual tables
        db_from_tables = self.db_from.tables if self.db_from else {}
        ( tables_to_add, tables_to_remove, tables_to_update ) = self._differences( db_from_tables, self.db_to.tables )

        # IMPORTANT! tracking db and tables_to_add are both passed in by reference
        # (like everything in python), but in this case I actually modify them by reference.
        # not my preference, but it makes it easier here
        ( errors_1215, operations ) = self._process_adds( tracking_db, tables_to_add )

        # if we have errors we are done
        if errors_1215:
            return [ errors_1215, operations ]

        # now apply table updates.  This acts differently: it returns a dictionary with
        # two sets of operations: one to update the tables themselves, and one to update
        # the foreign keys.  The latter are applied after everything else has happened.
        fk_operations = []
        split_operations = self._process_updates( tracking_db, tables_to_update )
        if split_operations['kitchen_sink']:
            operations.extend( split_operations['kitchen_sink'] )
        if split_operations['fks']:
            fk_operations.extend( split_operations['fks'] )

        # now that we got some tables modified let's try adding tables again
        # if we have any left.  Remember that tracking_db and tables_to_add
        # are modified in-place.  The point here is that there may be some
        # tables to add that we were not able to add before because they
        # relied on adding a column to a table before a foreign key could
        # be supported.
        if tables_to_add:
            ( errors_1215, more_operations ) = self._process_adds( tracking_db, tables_to_add )
            if more_operations:
                operations = operations.extend( more_operations )
            if errors_1215:
                if fk_operations:
                    operations.extend( fk_operations )
                retrun [ errors_1215, operations ]

        # At this point in time if we still have tables to add it is because
        # they have mutually-dependent foreign key constraints.  The way to
        # fix that is to be a bit smarter (but not too smart) and remove
        # from the tables all foreign key constraints that can't be added yet.
        # Then run the CREATE TABLE operations, and add the foreign key
        # constraints afterward
        for table_to_add in tables_to_add:
            new_table = self.db_to.tables[table_to_add]
            bad_constraints = tracking_db.unfulfilled_fks( new_table )
            new_table_copy = copy.deepcopy( new_table )
            create_fks = alter_table( table_to_add )
            for constraint in bad_constraints.values():
                create_fks.add_operation( add_constraint( constraint['foreign_key'] ) )
                new_table_copy.remove_constraint( constraint['foreign_key'] )
            operations.append( new_table_copy.create() )
            fk_operations.append( create_fks )

        # go ahead and remove our tables
        for table_to_remove in tables_to_remove:
            operations.append( remove_table( table_to_remove ) )
            tracking_db.remove_table( table_to_remove )

        # then add back in our foreign key constraints
        if fk_operations:
            operations.extend( fk_operations )

        # all done!!!
        return [ errors_1215, operations ]

    def _process_adds( self, tracking_db, tables_to_add ):
        """ Runs through tables_to_add and resolves FK constraints to determine order to add tables in

        tracking_db and tables_to_add are passed in by reference and modified

        :returns: A list of 1215 error messages and a list of mygration operations
        :rtype: ( [{'error': string, 'foreign_key': mygrations.formats.mysql.definitions.constraint}], [mygrations.formats.mysql.mygrations.operations.operation] )
        """
        errors_1215 = []
        operations = []
        good_tables = {}

        # keep looping through tables as long as we find some to process
        # the while loop will stop under two conditions: if all tables
        # are processed or if we stop adding tables, which happens if we
        # have tables with mutualy-dependent foreign key constraints
        last_number_to_add = 0
        while tables_to_add and len( tables_to_add ) != last_number_to_add:
            last_number_to_add = len( tables_to_add )
            for new_table_name in tables_to_add:
                new_table = self.db_to.tables[new_table_name]
                bad_constraints = tracking_db.unfulfilled_fks( new_table )

                # if we found no problems then we can add this table to our
                # tracking db and add the "CREATE TABLE" operation to our list of operations
                if not bad_constraints:
                    tables_to_add.remove( new_table_name )
                    operations.append( new_table.create() )
                    tracking_db.add_table( new_table )
                    continue

                # the next question is whether this is a valid constraint
                # that simply can't be added yet (because it has dependencies
                # that have not been added) or if there is an actual problem
                # with the constraint.
                if new_table_name in good_tables:
                    continue

                # If we are here we have to decide if this table is fulfillable
                # eventually, or if there is a mistake with a foreign key that
                # we can't fix.  To tell the difference we just check if the
                # database we are migrating to can fulfill these foreign keys.
                broken_constraints = self.db_to.unfulfilled_fks( new_table )
                if not broken_constraints:
                    good_tables[new_table_name] = True
                    continue

                # otherwise it is no good: record as such
                tables_to_add.remove( new_table_name )
                for error in broken_constraints.values():
                    errors_1215.append( error['error'] )

        return ( errors_1215, operations )

    def _process_updates( self, tracking_db, tables_to_update ):
        """ Runs through tables_to_update and resolves FK constraints to determine order to add them in

        tracking_db is passed in by reference and modified

        This doesn't return a list of 1215 errors because those would have been
        Taken care of the first run through when the "to" database was mygrated
        by itself.  Instead, this separates alters and foreign key updates
        into different operations so the foreign key updates can be ran separately.

        :returns: a dict
        :rtype: {'fks': list, 'kitchen_sink': list}
        """

        tables_to_update = tables_to_update[:]

        operations = {
            'fks':          [],
            'kitchen_sink': []
        }

        for update_table_name in tables_to_update:
            target_table = self.db_to.tables[update_table_name]
            source_table = self.db_from.tables[update_table_name]

            more_operations = source_table.to( target_table, True )
            if 'fks' in more_operations:
                operations.extend( more_operations['fks'] )
                for operation in more_operations['fks']:
                    database.apply_operation( update_table_name, operation )
            if 'kitchen_sink' in more_operations:
                operations.extend( more_operations['kitchen_sink'] )
                for operation in more_operations['kitchen_sink']:
                    database.apply_operation( update_table_name, operation )

        return operations
