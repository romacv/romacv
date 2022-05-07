<?php

if (empty($_GET['name']) || !isset($_GET['name'])) {
    header( 'Location: /projects.php', true, 303 );
} else {
    include 'header.inc';
    include 'data.inc';
    $name = $_GET['name'];
    $thisData = [];
    foreach ($data as $iData) {
        $thisData = array_merge($thisData, $iData);
    }
    $thisData = $thisData[$name];
}


?>


    <main>
        <div class="container">
            <section class="project">
                <img class="img-fluid" src="../img/works/<?=$thisData['img']?>.jpg" alt="<?=$thisData['title']?>">
                <h2><?=$thisData['title']?></h2>
                <p><?=$thisData['desc']?></p>
                <?php if($thisData['link']) { ?>
                    <a href="<?=$thisData['link']?>" target="_blank" rel="noopener"><img src="../img/applestore.png"></a>
                <?php } ?>
                <?php if(isset($thisData['img2']) && !empty($thisData['img2'])) { ?>
                    <img class="img-fluid ext-img" src="../img/works/<?=$thisData['img2']?>.jpg" alt="<?=$thisData['title']?>">
                <?php } ?>
            </section>
            <hr>
        </div>
    </main>
<?php include 'footer.inc' ?>