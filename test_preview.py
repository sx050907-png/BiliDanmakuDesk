import unittest
from core import mapped_time
from preview import resize_local_scene, inserted_scene_ranges, insert_local_scene, adjusted_options, timeline_bins, align_from, original_time_at
from test_core import OPTIONS

class TimingTests(unittest.TestCase):
    def test_resize_and_move_existing_scene_keep_other_edits(self):
        original=insert_local_scene(OPTIONS,600.,630.)
        original=insert_local_scene(original,1230.,1245.)
        longer=resize_local_scene(original,0,600.,650.)
        self.assertEqual(inserted_scene_ranges(longer),[(600.,650.),(1250.,1265.)])
        self.assertEqual(mapped_time(599.,longer),599.)
        self.assertEqual(mapped_time(700.,longer),750.)
        moved=resize_local_scene(original,0,620.,650.)
        self.assertEqual(inserted_scene_ranges(moved),[(620.,650.),(1230.,1245.)])
        self.assertEqual(mapped_time(600.,moved),600.)
        self.assertEqual(mapped_time(620.,moved),650.)
        self.assertEqual(inserted_scene_ranges(original),[(600.,630.),(1230.,1245.)])
        with self.assertRaises(ValueError):resize_local_scene(original,0,1220.,1240.)
        with self.assertRaises(ValueError):resize_local_scene(original,0,630.,600.)

    def test_local_uncut_scene_leaves_gap_and_preserves_previous_comments(self):
        result=insert_local_scene(OPTIONS,600.,630.)
        self.assertEqual(mapped_time(599.,result),599.)
        self.assertEqual(mapped_time(600.,result),630.)
        self.assertEqual(mapped_time(700.,result),730.)
        self.assertEqual(inserted_scene_ranges(result),[(600.,630.)])
        self.assertFalse(any(600.<=mapped_time(t,result)<630. for t in range(550,650)))
        again=insert_local_scene(result,1230.,1245.)
        self.assertEqual(mapped_time(1199.,again),1229.)
        self.assertEqual(mapped_time(1200.,again),1245.)
        self.assertEqual(mapped_time(1400.,again),1445.)
        self.assertEqual(inserted_scene_ranges(again),[(600.,630.),(1230.,1245.)])
        with self.assertRaises(ValueError):insert_local_scene(OPTIONS,30.,30.)
        with self.assertRaises(ValueError):insert_local_scene(OPTIONS,30.,20.)

    def test_local_cut_marker_uses_current_mapping_and_handles_inserted_gap(self):
        options={**OPTIONS,'offset':2.,'rate':1.01,'segments':[{'start':600.,'offset':30.}]}
        self.assertAlmostEqual(original_time_at(103.,options),100.)
        self.assertEqual(original_time_at(620.,options),600.)
        self.assertAlmostEqual(original_time_at(840.,options),800.)
        source=original_time_at(840.,options)
        changed=align_from(options,source,mapped_time(source,options)+5)
        self.assertEqual(mapped_time(799.,changed),mapped_time(799.,options))
        self.assertEqual(mapped_time(900.,changed),mapped_time(900.,options)+5)

    def test_cut_alignment_keeps_earlier_comments_and_existing_later_edits(self):
        original = {**OPTIONS, 'rate':1.01, 'offset':2., 'segments':[
            {'start':50., 'offset':4.}, {'start':900., 'offset':14.}]}
        changed = align_from(original, 600., 650.)
        delta = 650.-mapped_time(600.,original)
        for seconds in (0.,49.,50.,599.999):
            self.assertEqual(mapped_time(seconds,changed),mapped_time(seconds,original))
        for seconds in (600.,700.,900.,1200.):
            self.assertAlmostEqual(mapped_time(seconds,changed)-mapped_time(seconds,original),delta)
        self.assertEqual(mapped_time(600.,changed),650.)
        self.assertEqual(original['segments'][1]['offset'],14.)

    def test_multiple_cuts_and_replacing_same_anchor_do_not_double_count(self):
        first = align_from(OPTIONS,600.,630.)
        second = align_from(first,1200.,1245.)
        self.assertEqual(mapped_time(500.,second),500.)
        self.assertEqual(mapped_time(800.,second),830.)
        self.assertEqual(mapped_time(1400.,second),1445.)
        replaced = align_from(second,600.,620.)
        self.assertEqual(len(replaced['segments']),2)
        self.assertEqual(mapped_time(800.,replaced),820.)
        self.assertEqual(mapped_time(1400.,replaced),1435.)

    def test_local_shorter_version_moves_only_later_comments_earlier(self):
        changed = align_from(OPTIONS,600.,580.)
        self.assertEqual(mapped_time(599.,changed),599.)
        self.assertEqual(mapped_time(700.,changed),680.)
        with self.assertRaises(ValueError):
            align_from(OPTIONS,-1.,10.)

    def test_positive_delay_matches_export_with_segment_rule(self):
        options={**OPTIONS,'offset':2.,'rate':1.01,'segments':[{'start':60.,'offset':-3.}]}
        changed=adjusted_options(options,.5)
        for t in (10.,100.):
            self.assertAlmostEqual(mapped_time(t,changed)-mapped_time(t,options),.5)
        self.assertEqual(options['offset'],2.)
        self.assertEqual(changed['rate'],1.01)

    def test_early_and_late_cancel_without_mutating_original(self):
        early=adjusted_options(OPTIONS,.5)
        restored=adjusted_options(early,-.5)
        self.assertEqual(restored,OPTIONS)
        self.assertIsNot(restored,OPTIONS)

    def test_distribution_uses_movie_time_including_rate_and_segment_offset(self):
        rows = [{'progress_ms':t*1000, 'content':str(t)} for t in (0, 5, 20, 50)]
        options = {**OPTIONS, 'rate':2., 'offset':-2.,
                   'segments':[{'start':20., 'offset':12.}]}
        bins = timeline_bins(rows, options, 100., 10)
        # Movie times are -2, 8, 50 and 110: exclude comments outside the movie.
        self.assertEqual([len(group) for group in bins], [1,0,0,0,0,1,0,0,0,0])
        shifted = timeline_bins(rows, adjusted_options(options, 10.), 100., 10)
        self.assertEqual([len(group) for group in shifted], [1,1,0,0,0,0,1,0,0,0])

    def test_distribution_empty_duration_and_end_boundary(self):
        rows = [{'progress_ms':0}, {'progress_ms':99999}, {'progress_ms':100000}]
        options = {**OPTIONS, 'rate':1., 'offset':0., 'segments':[]}
        self.assertEqual(timeline_bins(rows, options, 0., 4), [[],[],[],[]])
        bins = timeline_bins(rows, options, 100., 4)
        self.assertEqual([len(group) for group in bins], [1,0,0,1])

if __name__=='__main__': unittest.main()
