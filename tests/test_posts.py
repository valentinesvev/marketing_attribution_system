from experiments.posts import classify_post


def test_percentages_and_company_names_do_not_imply_discount():
    assert classify_post("В Т-Банк берут 70% стажёров, вероятность успеха 90%")['post_type'] == 'regular'
    assert classify_post("Скидываем разбор собеседования")['post_type'] == 'regular'


def test_discount_takes_priority_but_keeps_launch_flag():
    result = classify_post("Запускаем курс. Скидка 30%")
    assert result['post_type'] == 'discount'
    assert result['is_course_launch']


def test_reviewed_launch_and_external_offer():
    assert classify_post("Мы запускаем ПРО", 1859)['post_type'] == 'course_launch'
    assert classify_post("Скидка на магистратуру", 1834)['offer_scope'] == 'external'
